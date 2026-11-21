"""
Durable reconciliation: finishes work that a crash, restart, or failed
BackgroundTask left half-done. Called once at app startup and at the top of
every worker cycle, so an accepted webhook always reaches the agent even if
the process died between "webhook recorded" and "agent finished".

Three stranded states are repaired:

1. webhook_events stuck in 'received'/'reconciling'
   The delivery was accepted (2xx returned) but the business rows were never
   committed. The stored payload is re-driven through the same ingestion path;
   the handlers are idempotent, so replay converges instead of duplicating.

2. recovery_agent_decisions with no recovery_agent_actions row
   The agent was interrupted between deciding and acting. The opportunity is
   re-run through the graph, which reuses the existing decision (see
   persist_decision) and completes the dispatch or review handoff.

3. recovery_agent_actions stuck in 'processing'
   A dispatch was claimed but never finalized (crash between claim and
   outcome write). What happens next depends on the mode:

   - DRY_RUN: the "send" only logs, so replay is always safe. The claim is
     re-taken atomically and the dispatch replayed through
     agent_graph.dispatch_claimed_decision from the stored message body.
   - LIVE: the provider may have accepted the crashed call. Twilio
     messages.create has no idempotency key and the crashed run never
     recorded a provider message id, so acceptance cannot be verified from
     here — and an automatic resend could duplicate a real customer message.
     The same applies to payment-link creation: a crashed run may already
     have created a real Razorpay link, and a blind replay would mint a
     second one. Live mode therefore NEVER auto-resends: the action is
     marked 'delivery_unknown' with an audit row and left for a human to
     reconcile against the provider dashboard before any retry.

Rows younger than `stale_after_seconds` are left alone: a row that fresh is
probably being processed right now by a live request, and touching it would
double-send. Timestamps are compared as ISO-8601 strings, which the schema
uses throughout.
"""

import logging
from datetime import datetime, timedelta, timezone

import agent_graph
import db as db_module
import ingestion
from config import settings

logger = logging.getLogger("revenue_recovery.reconcile")

DEFAULT_STALE_AFTER_SECONDS = 60


def reconcile_pending(stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
                      trigger_agent: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=stale_after_seconds)).isoformat()
    stats = {
        "webhook_events_redriven": 0,
        "decisions_completed": 0,
        "dispatches_replayed": 0,
        "dispatches_flagged_unknown": 0,
        "failures": 0,
    }

    # --- 1. stranded webhook events -------------------------------------
    conn = db_module.get_conn()
    try:
        pending_events = conn.execute(
            "SELECT * FROM webhook_events "
            "WHERE status IN ('received', 'reconciling') AND received_at < ?",
            (cutoff,),
        ).fetchall()
        for row in pending_events:
            cur = conn.execute(
                "UPDATE webhook_events SET status = 'reconciling' "
                "WHERE webhook_event_id = ? AND status IN ('received', 'reconciling')",
                (row["webhook_event_id"],),
            )
            conn.commit()
            if cur.rowcount == 0:
                continue  # someone else claimed it
            try:
                result = ingestion.process_webhook_event_record(conn, row)
                stats["webhook_events_redriven"] += 1
                opportunity_id = result.get("opportunity_id")
                if trigger_agent and opportunity_id:
                    agent_graph.run_recovery_for_opportunity(opportunity_id)
            except Exception as exc:
                logger.exception("Re-drive failed for webhook event %s",
                                 row["webhook_event_id"])
                try:
                    ingestion.mark_webhook_event(conn, row["webhook_event_id"],
                                                 "failed", str(exc)[:500])
                except Exception:
                    logger.exception("Could not mark webhook event %s failed",
                                     row["webhook_event_id"])
                stats["failures"] += 1

        # --- 2. decisions that never reached an action -------------------
        orphan_decisions = conn.execute(
            """
            SELECT rad.decision_id, rad.recovery_opportunity_id
            FROM recovery_agent_decisions rad
            LEFT JOIN recovery_agent_actions raa ON raa.decision_id = rad.decision_id
            WHERE raa.action_id IS NULL AND rad.decided_at < ?
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for row in orphan_decisions:
        try:
            if trigger_agent:
                agent_graph.run_recovery_for_opportunity(row["recovery_opportunity_id"])
            stats["decisions_completed"] += 1
        except Exception:
            logger.exception("Reconciliation agent run failed for opportunity %s",
                             row["recovery_opportunity_id"])
            stats["failures"] += 1

    # --- 3. dispatches claimed but never finalized ------------------------
    claimed_actions = []
    conn = db_module.get_conn()
    try:
        stale_actions = conn.execute(
            "SELECT action_id, decision_id FROM recovery_agent_actions "
            "WHERE status = 'processing' AND executed_at < ?",
            (cutoff,),
        ).fetchall()
        for row in stale_actions:
            cur = conn.execute(
                "UPDATE recovery_agent_actions SET executed_at = ? "
                "WHERE action_id = ? AND status = 'processing'",
                (now.isoformat(), row["action_id"]),
            )
            conn.commit()
            if cur.rowcount:
                claimed_actions.append(row)
            # rowcount == 0: finalized (or re-claimed) concurrently; leave it
    finally:
        conn.close()

    for row in claimed_actions:
        if not settings.dry_run:
            # LIVE: provider acceptance of the crashed call is unknowable
            # from here (no idempotency key, no provider message id recorded).
            # Flag for manual reconciliation; never auto-resend.
            conn = db_module.get_conn()
            try:
                conn.execute(
                    "UPDATE recovery_agent_actions SET status = 'delivery_unknown', "
                    "failure_reason = ? WHERE action_id = ? AND status = 'processing'",
                    ("send interrupted; provider acceptance unknown — reconcile "
                     "against the provider dashboard before any retry",
                     row["action_id"]),
                )
                agent_graph._audit(
                    conn, decision_id=row["decision_id"], action_id=row["action_id"],
                    event_type="dispatch_delivery_unknown",
                    before_state="processing",
                    after_state="delivery_unknown: manual reconciliation required",
                )
                conn.commit()
            finally:
                conn.close()
            stats["dispatches_flagged_unknown"] += 1
            logger.warning(
                "Decision %s: interrupted live dispatch marked delivery_unknown; "
                "manual reconciliation required", row["decision_id"])
            continue
        try:
            result = agent_graph.dispatch_claimed_decision(
                row["decision_id"], row["action_id"], audit_action="dispatch_replayed")
            stats["dispatches_replayed"] += 1
            logger.info("Replayed interrupted dispatch for decision %s -> %s",
                        row["decision_id"], result.get("status"))
        except Exception:
            logger.exception("Dispatch replay failed for decision %s", row["decision_id"])
            stats["failures"] += 1

    if any(v for k, v in stats.items() if k != "failures") or stats["failures"]:
        logger.info("Reconciliation pass: %s", stats)
    return stats
