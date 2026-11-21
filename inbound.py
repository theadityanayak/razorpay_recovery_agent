"""
inbound.py — the return leg of the closed loop: customer replies and
delivery receipts.

Two Twilio-facing flows live here, plus the shared business handlers so the
real webhooks and the /simulate/reply simulator exercise exactly the same
code path:

  handle_inbound_message  — a customer replied (or texted STOP) to a
                            recovery message. STOP flips the customer's
                            opt-out state (the compliance guardrail already
                            honors it); START reverses it; anything else is
                            recorded as a reply so response analytics and
                            the audit trail see it.

  handle_status_callback  — Twilio delivery receipt (queued/sent/delivered/
                            failed/undelivered) matched back to the
                            outbound_dispatches row by provider message id.

Signature verification mirrors the Razorpay posture: when the Twilio auth
token is configured, X-Twilio-Signature is validated (HMAC-SHA1 of the URL
+ sorted POST params, per Twilio's spec); when it isn't, only loopback
clients are answered, so a deployed instance cannot be fed forged inbound
events from the internet.

Delivery-receipt policy: a 'failed'/'undelivered' receipt marks the dispatch
row and writes an audit event, but NEVER auto-resends — an uncertain
delivery is a human reconciliation decision, not a retry trigger.
"""

import base64
import hashlib
import hmac
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import settings
from ingestion import _normalize_digits

logger = logging.getLogger("recovery_inbound")

# Twilio's opt-out keyword set (it also handles these at the network level
# for SMS, but WhatsApp replies and simulated replies arrive here).
OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
OPT_IN_KEYWORDS = {"start", "unstop", "yes"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_twilio_signature(
    *,
    url: str,
    params: dict,
    signature: Optional[str],
    client_host: Optional[str],
    loopback_hosts: set,
) -> bool:
    """Validates X-Twilio-Signature per Twilio's spec: HMAC-SHA1 over the
    full request URL with each POST param (sorted by key) appended as
    key+value, base64-encoded, keyed by the account auth token.

    Returns True when cryptographically verified, False when accepted via
    the loopback-only fallback (no auth token configured). Raises 401/503
    HTTPException when the request must be rejected — same posture as the
    Razorpay webhook verifier in main.py.
    """
    from fastapi import HTTPException

    loopback = client_host in loopback_hosts
    if settings.twilio_auth_token:
        if not signature:
            raise HTTPException(status_code=401, detail="Missing X-Twilio-Signature header.")
        data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        expected = base64.b64encode(
            hmac.new(settings.twilio_auth_token.encode(), data.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid Twilio webhook signature.")
        return True
    if loopback:
        logger.warning("Twilio webhook accepted without verification (no auth token configured; loopback only).")
        return False
    raise HTTPException(
        status_code=503,
        detail="TWILIO_AUTH_TOKEN is not configured; refusing unverifiable Twilio webhooks.",
    )


def _find_customer_by_phone(conn: sqlite3.Connection, from_handle: str):
    """Matches an inbound phone (Twilio sends E.164, sometimes prefixed with
    'whatsapp:') to a customer by digits-normalized phone, same rule the
    webhook ingester uses when creating customers."""
    digits = _normalize_digits(from_handle)
    if not digits:
        return None
    for row in conn.execute("SELECT customer_id, phone FROM customers").fetchall():
        if _normalize_digits(row["phone"]) == digits:
            return row["customer_id"]
    return None


def _latest_opportunity_for_customer(conn: sqlite3.Connection, customer_id: str):
    return conn.execute(
        """
        SELECT opportunity_id FROM recovery_opportunities
        WHERE customer_id = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (customer_id,),
    ).fetchone()


def handle_inbound_message(
    conn: sqlite3.Connection,
    *,
    from_handle: str,
    body: str,
    provider_message_id: Optional[str],
    channel: str,
) -> dict:
    """Records one inbound customer message and applies its effect.

    Idempotent on provider_message_id (Twilio retries webhook deliveries);
    a replayed MessageSid is a no-op. STOP/START flip the opt-out state via
    customer_contact_history, which the compliance guardrail reads before
    every dispatch — an opted-out customer is never contacted again, and
    the change takes effect on the very next agent run.
    """
    received_at = _now_iso()
    text = (body or "").strip()
    lowered = text.lower()
    is_opt_out = lowered in OPT_OUT_KEYWORDS
    is_opt_in = lowered in OPT_IN_KEYWORDS

    customer_id = _find_customer_by_phone(conn, from_handle)
    inbound_id = str(uuid.uuid4())
    try:
        conn.execute(
            """
            INSERT INTO inbound_messages (
                inbound_message_id, customer_id, channel, from_handle, body,
                provider_message_id, is_opt_out, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inbound_id, customer_id, channel, from_handle, text[:2000],
                provider_message_id, 1 if is_opt_out else 0, received_at,
            ),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"status": "duplicate", "provider_message_id": provider_message_id}

    if customer_id is None:
        conn.commit()
        logger.info("Inbound %s from unmatched handle (recorded for audit).", channel)
        return {"status": "recorded", "matched_customer": False, "inbound_message_id": inbound_id}

    effect = "reply"
    if is_opt_out or is_opt_in:
        # Opt state lives in customer_contact_history because that is what
        # the guardrail has always read; opt_out_flag on the LATEST opt row
        # is the customer's current state.
        opp = _latest_opportunity_for_customer(conn, customer_id)
        conn.execute(
            """
            INSERT INTO customer_contact_history (
                contact_history_id, customer_id, intervention_id,
                channel, message_status, opt_out_flag, contacted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), customer_id, None, channel,
                "opt_out" if is_opt_out else "opt_in",
                1 if is_opt_out else 0, received_at,
            ),
        )
        effect = "opt_out" if is_opt_out else "opt_in"
        conn.execute(
            """
            INSERT INTO recovery_audit_logs (
                audit_log_id, decision_id, action_id, event_type,
                before_state, after_state, logged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), None, None,
                "customer_opted_out" if is_opt_out else "customer_opted_in",
                None,
                json.dumps({
                    "customer_id": customer_id,
                    "channel": channel,
                    "opportunity_id": opp["opportunity_id"] if opp else None,
                    "keyword": lowered,
                }),
                received_at,
            ),
        )
    else:
        # A real reply: mark the latest contact as answered so response-rate
        # analytics and any future follow-up logic can see it.
        opp = _latest_opportunity_for_customer(conn, customer_id)
        conn.execute(
            """
            INSERT INTO customer_contact_history (
                contact_history_id, customer_id, intervention_id,
                channel, message_status, opt_out_flag, contacted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), customer_id, None, channel, "replied", 0, received_at),
        )
        conn.execute(
            """
            INSERT INTO recovery_audit_logs (
                audit_log_id, decision_id, action_id, event_type,
                before_state, after_state, logged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), None, None, "customer_replied",
                None,
                json.dumps({
                    "customer_id": customer_id,
                    "channel": channel,
                    "opportunity_id": opp["opportunity_id"] if opp else None,
                }),
                received_at,
            ),
        )

    conn.commit()
    return {
        "status": "processed",
        "matched_customer": True,
        "customer_id": customer_id,
        "effect": effect,
        "inbound_message_id": inbound_id,
    }


def handle_status_callback(
    conn: sqlite3.Connection,
    *,
    provider_message_id: str,
    message_status: str,
) -> dict:
    """Applies a Twilio delivery receipt to the outbound_dispatches row that
    carries this provider message id.

    terminal 'delivered'/'failed'/'undelivered' states are written together
    with an audit event; intermediate states (queued/sent) just update the
    row. A failed/undelivered receipt NEVER triggers a resend — it surfaces
    on the dispatch row and in /metrics for human reconciliation, the same
    gate as reconcile.py's delivery_unknown path.
    """
    now = _now_iso()
    row = conn.execute(
        "SELECT dispatch_id, action_id, delivery_status FROM outbound_dispatches "
        "WHERE provider_message_id = ?",
        (provider_message_id,),
    ).fetchone()
    if row is None:
        # Receipt for a message we did not send (or a dry-run id): acknowledge
        # so Twilio stops retrying, but record nothing.
        return {"status": "ignored", "reason": "no dispatch with this provider message id"}

    conn.execute(
        "UPDATE outbound_dispatches SET delivery_status = ?, delivery_status_at = ? "
        "WHERE dispatch_id = ?",
        (message_status, now, row["dispatch_id"]),
    )
    if message_status in ("delivered", "failed", "undelivered"):
        conn.execute(
            """
            INSERT INTO recovery_audit_logs (
                audit_log_id, decision_id, action_id, event_type,
                before_state, after_state, logged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), None, row["action_id"],
                f"delivery_{message_status}",
                row["delivery_status"],
                json.dumps({"dispatch_id": row["dispatch_id"],
                            "provider_message_id": provider_message_id}),
                now,
            ),
        )
    conn.commit()
    return {
        "status": "processed",
        "dispatch_id": row["dispatch_id"],
        "delivery_status": message_status,
        "terminal": message_status in ("delivered", "failed", "undelivered"),
    }
