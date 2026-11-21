"""
Webhook ingestion: normalisation, idempotent persistence, and re-drive.

Separated from main.py so the reconciler (reconcile.py) can replay a stored
webhook event through exactly the same code path that handled it live. Every
handler here is safe to run twice: the unique keys on webhook_events stop
duplicate deliveries, and each business handler checks for the rows it would
create before creating them.

Event lifecycle (durable, crash-safe):

    received    row written + committed before any business row exists
    reconciling a reconciler claimed the row and is re-driving it now
    processed   business rows committed; nothing left to do
    failed      handler raised; detail carries the error, row is replayable
    ignored     event type this pipeline does not act on

A crash between "received" and "processed" leaves the row claimable by
reconcile.reconcile_pending() on the next startup or worker cycle.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

import db as db_module
from config import settings

FAILURE_EVENTS = {
    "payment.failed", "subscription.halted", "invoice.expired",
    # Revenue clawed back after the fact: money is at risk again.
    "refund.processed", "dispute.created",
}
RECOVERY_EVENTS = {
    "payment.captured",
    # The recovery link we sent was paid; a subscription charge succeeded.
    "payment_link.paid", "subscription.charged",
}


def _event_entity(payload: dict) -> tuple[str, dict, Optional[str]]:
    """Extracts (event_type, entity, payment_id) from a Razorpay webhook body."""
    event_type = payload.get("event", "")
    inner = payload.get("payload", {}) or {}
    entity = {}
    payment_id = None
    if "payment" in inner:
        entity = inner["payment"].get("entity", {}) or {}
        payment_id = entity.get("id")
    elif "payment_link" in inner:
        entity = inner["payment_link"].get("entity", {}) or {}
        payment_id = entity.get("payment_id")
    elif "refund" in inner:
        entity = inner["refund"].get("entity", {}) or {}
        # The refund id (not the original payment id) keys idempotency: one
        # payment can produce several refunds, each a distinct risk event.
        payment_id = entity.get("id")
    elif "dispute" in inner:
        entity = inner["dispute"].get("entity", {}) or {}
        payment_id = entity.get("id")
    elif "subscription" in inner:
        entity = inner["subscription"].get("entity", {}) or {}
    elif "invoice" in inner:
        entity = inner["invoice"].get("entity", {}) or {}
        payment_id = entity.get("payment_id")
    return event_type, entity, payment_id


def _normalize_digits(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def _resolve_merchant_id(conn) -> str:
    """Webhooks arrive per Razorpay account, not per merchant row. Use the
    configured DEFAULT_MERCHANT_ID; if unset and the DB holds exactly one
    merchant, use it; otherwise refuse loudly instead of guessing."""
    if settings.default_merchant_id:
        return settings.default_merchant_id
    rows = conn.execute("SELECT merchant_id FROM merchants").fetchall()
    if len(rows) == 1:
        return rows[0]["merchant_id"]
    raise HTTPException(
        status_code=503,
        detail="Set DEFAULT_MERCHANT_ID in .env — the database has "
               f"{len(rows)} merchants, so webhook events cannot be attributed automatically.",
    )


def _resolve_or_create_customer(conn, merchant_id: str, email, phone, name) -> str:
    """Matches an existing customer by email or phone (digits-normalized);
    creates a minimal record otherwise so the FK chain always resolves."""
    if email:
        row = conn.execute(
            "SELECT customer_id FROM customers WHERE merchant_id = ? AND lower(email) = lower(?)",
            (merchant_id, email),
        ).fetchone()
        if row:
            return row["customer_id"]
    digits = _normalize_digits(phone)
    if digits:
        for row in conn.execute(
            "SELECT customer_id, phone FROM customers WHERE merchant_id = ?", (merchant_id,)
        ).fetchall():
            if _normalize_digits(row["phone"]) == digits:
                return row["customer_id"]

    customer_id = str(uuid.uuid4())
    parts = (name or "").split(None, 1)
    conn.execute(
        """
        INSERT INTO customers (
            customer_id, merchant_id, first_name, last_name, email, phone,
            city, state, customer_segment, customer_tenure_days,
            preferred_payment_method, communication_preference,
            optimal_contact_hour_ist, avg_response_delay_mins,
            price_sensitivity_score, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            customer_id, merchant_id,
            parts[0] if parts else "Unknown",
            parts[1] if len(parts) > 1 else "",
            email, phone,
            None, None, "webhook_ingested", 0,
            None, None, None, None, None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return customer_id


def _ensure_order(conn, order_id: Optional[str], merchant_id: str, customer_id: str,
                  amount_inr: float, currency: str, status: str) -> None:
    if not order_id:
        return
    exists = conn.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if exists:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO orders (
            order_id, merchant_id, customer_id, product_id, order_amount,
            currency, order_type, order_status, created_at,
            checkout_started_at, checkout_completed_at, payment_deadline
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, merchant_id, customer_id, None, amount_inr, currency,
         "payment", status, now, now, None, None),
    )


def _record_webhook_event(conn, *, event_id, event_type, payment_id,
                          signature_verified, detail, payload) -> Optional[str]:
    """Inserts the idempotency record in 'received' state (processed_at NULL).
    Returns the new webhook_event_id, or None when this delivery is a duplicate
    (same event_id, or same payment_id + event_type) and nothing further
    should happen."""
    webhook_event_id = str(uuid.uuid4())
    try:
        conn.execute(
            """
            INSERT INTO webhook_events (
                webhook_event_id, event_id, event_type, payment_id,
                signature_verified, status, detail, payload_json,
                received_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, 'received', ?, ?, ?, NULL)
            """,
            (
                webhook_event_id, event_id, event_type, payment_id,
                1 if signature_verified else 0, detail,
                json.dumps(payload)[:20000],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return webhook_event_id
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


def mark_webhook_event(conn, webhook_event_id: str, status: str,
                       detail: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE webhook_events SET status = ?, detail = COALESCE(?, detail), "
        "processed_at = ? WHERE webhook_event_id = ?",
        (status, detail, datetime.now(timezone.utc).isoformat(), webhook_event_id),
    )
    conn.commit()


def _handle_failure_event(conn, event_type: str, entity: dict, payment_id: Optional[str]) -> str:
    """Creates the risk event + opportunity for a failure webhook and returns
    the opportunity_id. Idempotent: if this payment (or subscription/invoice
    event) already produced a risk event, the existing opportunity is
    returned and no rows are duplicated."""
    original_payment_id = None
    if event_type == "payment.failed":
        mapped_event = "payment_failed"
        root_cause = (entity.get("error_code") or "payment_failed").lower()
        subscription_id = entity.get("subscription_id")
        invoice_id = None
    elif event_type == "subscription.halted":
        mapped_event = "subscription_halted"
        root_cause = "subscription_halted"
        subscription_id = entity.get("id")
        invoice_id = None
    elif event_type == "refund.processed":
        mapped_event = "refund_processed"
        root_cause = "refund_processed"
        subscription_id = None
        invoice_id = None
        original_payment_id = entity.get("payment_id")
    elif event_type == "dispute.created":
        mapped_event = "dispute_created"
        root_cause = "dispute_created"
        subscription_id = None
        invoice_id = None
        original_payment_id = entity.get("payment_id")
    else:  # invoice.expired
        mapped_event = "invoice_expired"
        root_cause = "invoice_expired"
        subscription_id = None
        invoice_id = entity.get("id")

    # Replay guard: find the opportunity this event already created, if any.
    existing = None
    if payment_id:
        existing = conn.execute(
            """
            SELECT ro.opportunity_id FROM revenue_risk_events rre
            JOIN recovery_opportunities ro ON ro.revenue_risk_event_id = rre.risk_event_id
            WHERE rre.external_payment_id = ? AND rre.event_type = ?
            """,
            (payment_id, mapped_event),
        ).fetchone()
    if existing is None and subscription_id:
        existing = conn.execute(
            """
            SELECT ro.opportunity_id FROM revenue_risk_events rre
            JOIN recovery_opportunities ro ON ro.revenue_risk_event_id = rre.risk_event_id
            WHERE rre.subscription_id = ? AND rre.event_type = ?
            """,
            (subscription_id, mapped_event),
        ).fetchone()
    if existing is None and invoice_id:
        existing = conn.execute(
            """
            SELECT ro.opportunity_id FROM revenue_risk_events rre
            JOIN recovery_opportunities ro ON ro.revenue_risk_event_id = rre.risk_event_id
            WHERE rre.invoice_id = ? AND rre.event_type = ?
            """,
            (invoice_id, mapped_event),
        ).fetchone()
    if existing:
        return existing["opportunity_id"]

    # Refunds/disputes carry no customer contact fields; inherit the customer,
    # merchant and order from the risk event the ORIGINAL payment produced,
    # when we have it, before falling back to fresh resolution.
    prior = None
    if original_payment_id:
        prior = conn.execute(
            "SELECT customer_id, merchant_id, order_id, subscription_id "
            "FROM revenue_risk_events WHERE external_payment_id = ? LIMIT 1",
            (original_payment_id,),
        ).fetchone()

    merchant_id = prior["merchant_id"] if prior else _resolve_merchant_id(conn)
    amount_inr = (entity.get("amount") or 0) / 100.0
    currency = entity.get("currency") or "INR"

    if prior:
        customer_id = prior["customer_id"]
    else:
        customer_id = _resolve_or_create_customer(
            conn,
            merchant_id,
            email=entity.get("email") or entity.get("customer_email"),
            phone=entity.get("contact") or entity.get("customer_contact"),
            name=(entity.get("notes") or {}).get("customer_name") if isinstance(entity.get("notes"), dict) else None,
        )
    order_id = entity.get("order_id") or (prior["order_id"] if prior else None)
    if subscription_id is None and prior:
        subscription_id = prior["subscription_id"]
    _ensure_order(conn, order_id, merchant_id, customer_id, amount_inr, currency, status="failed")

    risk_event_id = str(uuid.uuid4())
    opportunity_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO revenue_risk_events (
            risk_event_id, order_id, subscription_id, invoice_id,
            customer_id, merchant_id, event_type, root_cause,
            amount_at_risk, recoverability_ground_truth, detected_at, notes,
            external_payment_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            risk_event_id, order_id, subscription_id, invoice_id,
            customer_id, merchant_id, mapped_event, root_cause,
            amount_inr, None, now,
            f"Ingested via Razorpay webhook ({event_type})",
            payment_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO recovery_opportunities (
            opportunity_id, revenue_risk_event_id, customer_id, merchant_id,
            eligible_for_contact, ineligible_reason, estimated_recovery_probability,
            recommended_intervention, estimated_value, intervention_cost, created_at,
            status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            opportunity_id, risk_event_id, customer_id, merchant_id,
            1, None, None, None, amount_inr, None, now, "open",
        ),
    )
    conn.commit()
    return opportunity_id


def _handle_captured_event(conn, entity: dict, payment_id: Optional[str],
                           reference_id: Optional[str] = None) -> int:
    """Matches a successful payment back to open recovery opportunities on
    the same order (or the same subscription / original failed payment) and
    records the recovered revenue. Returns how many opportunities closed.
    Safe to replay: opportunities already 'recovered' are filtered out and
    recovered_revenue is never double-booked for the same payment.

    reference_id carries the recovery opportunity id stamped onto the
    Razorpay payment link at creation time, so a payment_link.paid event
    attributes the recovery directly, no matching heuristic needed."""
    order_id = entity.get("order_id")
    subscription_id = entity.get("subscription_id")
    amount_inr = (entity.get("amount") or 0) / 100.0
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        """
        SELECT ro.opportunity_id, rre.amount_at_risk
        FROM recovery_opportunities ro
        JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id
        WHERE (ro.status IS NULL OR ro.status != 'recovered')
          AND (rre.order_id = ? OR rre.external_payment_id = ?
               OR (? IS NOT NULL AND rre.subscription_id = ?)
               OR (? IS NOT NULL AND ro.opportunity_id = ?))
        """,
        (order_id, payment_id, subscription_id, subscription_id,
         reference_id, reference_id),
    ).fetchall()

    closed = 0
    for row in rows:
        # Belt-and-braces idempotency: never double-book the same payment.
        dup = conn.execute(
            "SELECT 1 FROM recovered_revenue WHERE successful_payment_id = ? AND recovery_opportunity_id = ?",
            (payment_id, row["opportunity_id"]),
        ).fetchone()
        if dup:
            continue

        intervention = conn.execute(
            """
            SELECT intervention_id FROM recovery_interventions
            WHERE recovery_opportunity_id = ? AND status = 'executed'
            ORDER BY executed_at DESC LIMIT 1
            """,
            (row["opportunity_id"],),
        ).fetchone()
        link_dispatch = conn.execute(
            """
            SELECT 1 FROM outbound_dispatches
            WHERE recovery_opportunity_id = ? AND payment_link_url IS NOT NULL
            LIMIT 1
            """,
            (row["opportunity_id"],),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO recovered_revenue (
                recovered_revenue_id, recovery_opportunity_id, successful_payment_id,
                attributed_to_intervention_id, recovered_amount, original_amount_at_risk,
                recovery_method, attribution_confidence, recovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                row["opportunity_id"],
                payment_id,
                intervention["intervention_id"] if intervention else None,
                amount_inr,
                row["amount_at_risk"],
                "payment_link" if link_dispatch else "natural_recovery",
                "direct" if intervention else "none",
                now,
            ),
        )
        conn.execute(
            "UPDATE recovery_opportunities SET status = 'recovered', recovered_at = ?, "
            "recovered_payment_id = ? WHERE opportunity_id = ?",
            (now, payment_id, row["opportunity_id"]),
        )
        conn.execute(
            """
            INSERT INTO recovery_audit_logs (
                audit_log_id, decision_id, action_id, event_type,
                before_state, after_state, logged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), None, None, "payment_recovered",
                "open",
                json.dumps({
                    "opportunity_id": row["opportunity_id"],
                    "payment_id": payment_id,
                    "recovered_amount": amount_inr,
                    "attributed": bool(intervention),
                }),
                now,
            ),
        )
        closed += 1

    conn.commit()
    return closed


def process_webhook_event_record(conn, row) -> dict:
    """Drives one webhook_events row to 'processed' from its stored payload.

    Used by the live endpoint right after the row is recorded, and by
    reconcile.reconcile_pending() for rows stranded in 'received'. The
    business handlers are idempotent, so replaying a half-processed event
    converges instead of duplicating.
    """
    payload = json.loads(row["payload_json"] or "{}")
    event_type, entity, payment_id = _event_entity(payload)
    webhook_event_id = row["webhook_event_id"]

    if event_type in FAILURE_EVENTS:
        opportunity_id = _handle_failure_event(conn, event_type, entity, payment_id)
        mark_webhook_event(conn, webhook_event_id, "processed")
        return {"status": "processed", "event_type": event_type,
                "opportunity_id": opportunity_id}

    if event_type in RECOVERY_EVENTS:
        reference_id = entity.get("reference_id") if event_type == "payment_link.paid" else None
        closed = _handle_captured_event(conn, entity, payment_id, reference_id=reference_id)
        mark_webhook_event(conn, webhook_event_id, "processed",
                           f"closed {closed} opportunit{'y' if closed == 1 else 'ies'}")
        return {"status": "processed", "event_type": event_type, "closed": closed}

    mark_webhook_event(conn, webhook_event_id, "ignored", "unsupported event type")
    return {"status": "ignored", "event_type": event_type}
