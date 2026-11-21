"""
scheduler.py — the autonomous polling worker.

Two ways to run it, both sharing the same cycle:

  1. Integrated (default for the app): main.py's FastAPI lifespan starts
     worker_loop() as an asyncio background task, so a single command —
     `uvicorn main:app` — brings up the API, the webhook listener and the
     retry/polling loop together, and shuts them down together.

  2. Standalone (for demos or a separate process):
         python3 scheduler.py                          # poll every 60s
         python3 scheduler.py --interval 30
         python3 scheduler.py --interval 10 --max-cycles 3

What this deliberately does NOT do (so it doesn't overclaim what it is):
  - No distributed queue (Celery/Redis/RabbitMQ) — SQLite + polling is the
    honest match for this project's scale. A queue is the right upgrade
    only once webhook/event volume outgrows a single worker.
  - No opportunity-level retry loops — a failed opportunity is logged via
    DataIntegrityError's own decision/audit trail (see agent_graph.py) and
    is not silently retried forever. (Live provider sends DO retry
    connection-level failures with bounded backoff — see dispatchers.py —
    but never uncertain deliveries.)
  - No multi-process concurrency — SQLite allows ONE writer at a time.
    WAL mode + busy_timeout (see db.py) make reads and single-writer writes
    coexist smoothly, but they do not make concurrent writes scale; do not
    run multiple worker processes against one SQLite file.
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime

from agent_graph import run_batch

LOG_FILE = "recovery_agent.log"

logger = logging.getLogger("recovery_scheduler")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)


def run_worker_cycle(cycle: int) -> None:
    """One poll + batch. Isolated so a cycle-level failure (e.g. DB file
    temporarily locked) is logged and the next cycle still runs."""
    cycle_start = datetime.now()
    logger.info(f"--- Cycle {cycle} starting ---")
    try:
        summary = run_batch()
        logger.info(
            f"Cycle {cycle} complete. dispatched={summary.get('dispatched', 0)} "
            f"skipped={summary.get('skipped', 0)} "
            f"pending_review={summary.get('pending_review', 0)} "
            f"errors={summary.get('errors', 0)} "
            f"(took {(datetime.now() - cycle_start).total_seconds():.1f}s)"
        )
    except Exception as e:
        logger.error(f"Cycle {cycle} failed unexpectedly: {type(e).__name__}: {e}", exc_info=True)


async def worker_loop(stop_event: asyncio.Event, interval_seconds: int) -> None:
    """The always-on loop main.py hosts inside uvicorn.

    run_batch is synchronous (sqlite3 + LangGraph), so each cycle runs in a
    thread via asyncio.to_thread and the event loop stays free to serve
    API requests while a batch is in flight. Shutdown is graceful: the
    current cycle finishes, then the loop exits.
    """
    logger.info(f"Background worker started. Poll interval: {interval_seconds}s.")
    cycle = 0
    try:
        while not stop_event.is_set():
            cycle += 1
            await asyncio.to_thread(run_worker_cycle, cycle)
            try:
                # Wait for the next cycle, but wake immediately on shutdown.
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass  # normal: interval elapsed, run the next cycle
    finally:
        logger.info("Background worker stopped.")


class GracefulShutdown:
    """Catches SIGINT/SIGTERM and lets the current cycle finish instead of
    killing the process mid-batch, which could leave a partially-processed
    opportunity in an ambiguous state."""

    def __init__(self):
        self.shutdown_requested = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum, frame):
        logger.info(f"Shutdown signal received ({signal.Signals(signum).name}) — "
                    f"finishing current cycle, then exiting.")
        self.shutdown_requested = True


def run_scheduler(interval_seconds: int, max_cycles: int = None) -> None:
    """Standalone mode: same cycles as worker_loop, in a plain sync loop."""
    from db import init_db
    init_db()
    shutdown = GracefulShutdown()
    logger.info(f"Recovery agent scheduler starting. Poll interval: {interval_seconds}s. "
                f"Log file: {LOG_FILE}")

    cycle = 0
    while not shutdown.shutdown_requested:
        cycle += 1
        run_worker_cycle(cycle)

        if max_cycles is not None and cycle >= max_cycles:
            logger.info(f"Reached --max-cycles={max_cycles}. Stopping.")
            break

        if shutdown.shutdown_requested:
            break

        logger.info(f"Sleeping {interval_seconds}s until next cycle...")
        # Sleep in small increments so a shutdown signal during the sleep
        # is honored promptly instead of waiting out the full interval.
        slept = 0
        while slept < interval_seconds and not shutdown.shutdown_requested:
            time.sleep(min(1, interval_seconds - slept))
            slept += 1

    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll for undecided opportunities and run the recovery agent on an interval.")
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Seconds between poll cycles (default: 60).",
    )
    parser.add_argument(
        "--max-cycles", type=int, default=None,
        help="Stop after N cycles instead of running forever (useful for testing/demos).",
    )
    args = parser.parse_args()

    run_scheduler(interval_seconds=args.interval, max_cycles=args.max_cycles)
