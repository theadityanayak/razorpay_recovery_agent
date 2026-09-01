"""
scheduler.py — long-running process that polls for undecided recovery
opportunities on a fixed interval and runs them through the recovery agent.

This is the piece that turns agent_graph.py from "a script you run by hand"
into something that behaves like a real service: structured logging to a
file (not print statements), a configurable poll interval, and clean
shutdown on Ctrl+C or SIGTERM instead of dying mid-batch.

Usage:
    python3 scheduler.py                        # poll every 60s, run forever
    python3 scheduler.py --interval 30           # poll every 30s
    python3 scheduler.py --interval 10 --max-cycles 3   # for testing/demo — stop after N cycles

What this deliberately does NOT do (so it doesn't overclaim what it is):
  - No distributed queue (Celery/Redis/RabbitMQ) — SQLite + polling is the
    honest match for this project's scale. A queue is the right upgrade
    only once there's a real ingestion source producing events faster than
    a poll loop can keep up with.
  - No retry backoff — a failed opportunity is logged via
    DataIntegrityError's own decision/audit trail (see agent_graph.py) and
    is not reprocessed; it's not simply retried until it works.
  - No multi-process/concurrency — SQLite + a single polling loop is
    intentionally simple. Don't scale this to multiple workers without
    first moving off a single-file SQLite database (write contention will
    become a real bottleneck before scale is a real concern).
"""

import argparse
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
    shutdown = GracefulShutdown()
    logger.info(f"Recovery agent scheduler starting. Poll interval: {interval_seconds}s. "
                f"Log file: {LOG_FILE}")

    cycle = 0
    while not shutdown.shutdown_requested:
        cycle += 1
        cycle_start = datetime.now()
        logger.info(f"--- Cycle {cycle} starting ---")

        try:
            summary = run_batch()
            logger.info(
                f"Cycle {cycle} complete. dispatched={summary.get('dispatched', 0)} "
                f"skipped={summary.get('skipped', 0)} errors={summary.get('errors', 0)} "
                f"(took {(datetime.now() - cycle_start).total_seconds():.1f}s)"
            )
        except Exception as e:
            # A cycle-level failure (e.g. DB file locked/missing) should not
            # kill the scheduler itself — log it and try again next cycle.
            logger.error(f"Cycle {cycle} failed unexpectedly: {type(e).__name__}: {e}", exc_info=True)

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