"""
LangGraph Recovery Agent — wired to the real AI Revenue Recovery Platform schema.

FK chain this agent respects:
    revenue_risk_events
        -> recovery_opportunities   (revenue_risk_event_id)
            -> recovery_agent_decisions   (recovery_opportunity_id)
                -> recovery_agent_actions      (decision_id)
                -> recovery_attempts           (recovery_opportunity_id, + optional payment_attempt_id / contact_history_id)

Design decisions worth calling out (vs. the starter stub):
  1. Compliance guardrail reads the merchant's actual contact_policies row and
     customer_contact_history — not a hardcoded 21:00-09:00 window.
  2. Every path (approved AND blocked) writes a recovery_agent_decisions row.
     A blocked decision still gets a recovery_agent_actions row with
     status='skipped', so nothing vanishes from the audit trail.
  3. Root cause triage looks at payment_attempts.failure_code first (the
     granular gateway taxonomy), falling back to revenue_risk_events.root_cause
     when there's no payment_attempt (e.g. a pure subscription/mandate event).
  4. Mandate-aware routing: if the opportunity is tied to a subscription whose
     mandate requires_reauth, we route to a reauth strategy instead of a
     generic retry link.
  5. State carries every FK we need to insert child rows correctly
     (recovery_opportunity_id, decision_id) instead of only business fields.
  6. LLM message generation (generate_message_node) only runs on the
     policy-approved path — a blocked contact never gets a message drafted.
  7. Dispatch closes a real gap: a successful dispatch writes
     recovery_interventions + customer_contact_history +
     recovery_generated_messages together, and
     recovery_attempts.contact_history_id is populated instead of always NULL.

Closed-loop upgrade (v2):
  8. Pluggable dispatch (dispatchers.py): strategies that need a one-click
     retry create a Razorpay Payment Link, which is injected into the Gemini
     draft; the message goes out via Twilio (WhatsApp/SMS) or Resend (email).
     DRY_RUN=true (the default) runs the identical code path, logs the exact
     payloads, and writes the same audit rows with dry_run=1 — nothing is
     charged and nothing is sent.
  9. Human-in-the-loop: a policy-approved decision whose confidence is below
     settings.human_review_confidence_threshold is flagged
     requires_human_review=1 and its action is parked as 'pending_review'
     instead of dispatching. The dashboard's review queue resolves it through
     POST /decisions/{id}/approve (which dispatches via
     execute_human_approved_dispatch) or /reject (human_reject_decision).
 10. If GOOGLE_API_KEY is missing or the Gemini call fails, message drafting
     falls back to a deterministic template marked '[template]' so the
     pipeline still completes end-to-end and the audit trail says why.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any

from pydantic import BaseModel, Field
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END

from config import settings
from db import get_conn
from dispatchers import create_payment_link, send_message

logger = logging.getLogger("recovery_agent")

IST = ZoneInfo("Asia/Kolkata")


class DataIntegrityError(Exception):
    """Raised when a recovery_opportunities row references a customer_id or
    merchant_id that doesn't actually exist in customers/merchants. This
    agent never joins against those tables for its own business logic
    (guardrail keys off contact_policies.merchant_id, not merchants itself),
    so a dangling reference would otherwise be processed silently — a
    decision and dispatch would fire for a customer_id nothing else in the
    system can resolve. This is raised loudly instead, after logging a
    blocked decision + audit row, so the bad reference shows up in
    recovery_agent_decisions / recovery_audit_logs rather than only in a
    stack trace nobody sees until the messaging API call downstream fails.
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# --- MERCHANT TONE HEURISTIC ---
# NOTE: merchants has no real tone/brand-voice column — Tonic Fabricate
# never generated one. This is an inference from business_type + industry,
# not real merchant data. Every tone string returned here is prefixed
# "[heuristic]" wherever it's persisted, so nobody downstream (including a
# demo audience) mistakes it for actual merchant input.
_INDUSTRY_TONE = {
    "healthcare": "careful, reassuring, no urgency pressure — trust matters more than conversion",
    "SaaS": "professional, concise, solution-focused",
    "education": "warm, encouraging, patient",
    "food delivery": "casual, quick, friendly urgency",
    "travel": "helpful, reassuring, detail-oriented",
    "ecommerce": "friendly, upbeat, mild urgency",
    "retail": "friendly, upbeat, mild urgency",
    "subscriptions": "polite, low-pressure, value-reminding",
    "marketplaces": "neutral, transactional, brief",
    "digital services": "professional, clear, concise",
}

_BUSINESS_TYPE_MODIFIER = {
    "B2B": "more formal register than usual",
    "B2B2C": "moderately formal, still approachable",
    "Marketplace": "neutral, platform-voiced rather than personal",
    "D2C": "personal, brand-voiced",
    "B2C": "personal, warm",
}


def derive_merchant_tone(business_type: Optional[str], industry: Optional[str]) -> str:
    """Heuristic only — see module note above. Returns a short descriptor
    string to feed the LLM prompt, not a value read from real merchant data."""
    base = _INDUSTRY_TONE.get(industry, "neutral, polite, professional")
    modifier = _BUSINESS_TYPE_MODIFIER.get(business_type)
    tone = f"{base}; {modifier}" if modifier else base
    return f"[heuristic] {tone}"


# Strategies whose customer message should carry a one-click Razorpay
# Payment Link for instant retry.
LINK_STRATEGIES = {
    "send_one_click_retry_link",
    "delayed_smart_retry",
    "offer_bnpl_or_split_pay",
    "request_mandate_reauth",
}

# Friendly phrasing for the template fallback — the customer should never
# see a raw gateway error code.
_FRIENDLY_FAILURE = {
    "insufficient_funds": "the payment didn't go through due to insufficient funds",
    "expired_card": "the card on file has expired",
    "card_declined": "the card was declined by the bank",
    "incorrect_pin": "the card verification didn't succeed",
    "bank_outage": "your bank's systems were temporarily unreachable",
    "bank_server_error": "your bank's systems were temporarily unreachable",
    "gateway_timeout": "the payment timed out at the gateway",
    "network_timeout": "the payment timed out before it could complete",
    "npci_outage": "the UPI network was temporarily unavailable",
    "customer_cancelled": "the payment was cancelled before completing",
    "mandate_reauth_required": "your auto-pay mandate needs to be re-authorised",
}


# --- 1. STATE DEFINITION ---
class RecoveryState(TypedDict, total=False):
    # Input
    recovery_opportunity_id: str

    # Loaded from DB during ingestion
    risk_event_id: str
    order_id: Optional[str]
    subscription_id: Optional[str]
    invoice_id: Optional[str]
    customer_id: str
    merchant_id: str
    amount: float
    currency: str
    event_type: str
    risk_root_cause: str                    # revenue_risk_events.root_cause
    payment_failure_code: Optional[str]      # payment_attempts.failure_code (latest attempt)
    payment_method: Optional[str]
    mandate_requires_reauth: bool
    recommended_intervention: Optional[str]  # from recovery_opportunities
    estimated_recovery_probability: Optional[float]

    # Computed / evaluated
    root_cause_category: Optional[str]
    policy_approved: bool
    policy_rejection_reason: Optional[str]
    selected_strategy: Optional[str]
    chosen_channel: Optional[str]
    confidence_score: Optional[float]
    requires_human_review: bool

    # Payment link (created before drafting so the message can carry it)
    payment_link_url: Optional[str]
    payment_link_id: Optional[str]

    # LLM-generated message (only populated on the approved path)
    generated_message_body: Optional[str]
    generated_message_reasoning: Optional[str]
    tone_used: Optional[str]
    message_model_used: Optional[str]

    # Persistence FKs produced along the way
    decision_id: Optional[str]
    action_id: Optional[str]
    message_id: Optional[str]

    execution_status: Optional[str]          # 'dispatched' | 'skipped' | 'pending_review' | 'failed'


# --- 2. AGENT NODES ---

def _log_data_integrity_failure(conn: sqlite3.Connection, opportunity_id: str, reason: str) -> None:
    """Writes a blocked recovery_agent_decisions row + recovery_audit_logs
    row for an opportunity that failed the customer/merchant existence
    check, so the failure is visible in the same tables everything else
    lands in — not just a crash in the terminal."""
    decision_id = _new_id()
    conn.execute(
        """
        INSERT INTO recovery_agent_decisions (
            decision_id, recovery_opportunity_id, risk_assessment,
            estimated_recovery_probability, recommended_action,
            confidence_score, reasoning_summary, policy_check_result,
            requires_human_review, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            opportunity_id,
            "data_integrity_error",
            None,
            "data_integrity_error",
            None,
            reason,
            "blocked",
            1,
            _now_iso(),
        ),
    )
    conn.execute(
        """
        INSERT INTO recovery_audit_logs (
            audit_log_id, decision_id, action_id, event_type,
            before_state, after_state, logged_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id(),
            decision_id,
            None,
            "data_integrity_error",
            None,
            reason,
            _now_iso(),
        ),
    )
    conn.commit()


def _audit(conn: sqlite3.Connection, *, decision_id: Optional[str], action_id: Optional[str],
           event_type: str, before_state: Optional[str], after_state: Optional[str]) -> None:
    conn.execute(
        """
        INSERT INTO recovery_audit_logs (
            audit_log_id, decision_id, action_id, event_type,
            before_state, after_state, logged_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (_new_id(), decision_id, action_id, event_type, before_state, after_state, _now_iso()),
    )


def risk_event_ingestion_node(state: RecoveryState) -> Dict[str, Any]:
    """Loads the risk event + opportunity + latest payment attempt + mandate status.

    Entry point takes just `recovery_opportunity_id`; everything else is
    joined from the DB so the graph is never trusted to receive
    business fields directly from the caller.
    """
    conn = get_conn()
    try:
        opp = conn.execute(
            """
            SELECT ro.opportunity_id, ro.revenue_risk_event_id, ro.customer_id,
                   ro.merchant_id, ro.recommended_intervention,
                   ro.estimated_recovery_probability, ro.eligible_for_contact,
                   ro.ineligible_reason, ro.status
            FROM recovery_opportunities ro
            WHERE ro.opportunity_id = ?
            """,
            (state["recovery_opportunity_id"],),
        ).fetchone()
        if opp is None:
            raise ValueError(f"No recovery_opportunities row for {state['recovery_opportunity_id']}")

        # Referential integrity check: this agent never joins customers/
        # merchants for its own logic downstream (guardrail keys off
        # contact_policies, not merchants directly), so a dangling
        # customer_id or merchant_id would otherwise be processed silently.
        customer_exists = conn.execute(
            "SELECT 1 FROM customers WHERE customer_id = ?", (opp["customer_id"],)
        ).fetchone()
        merchant_exists = conn.execute(
            "SELECT 1 FROM merchants WHERE merchant_id = ?", (opp["merchant_id"],)
        ).fetchone()

        if customer_exists is None or merchant_exists is None:
            missing = []
            if customer_exists is None:
                missing.append(f"customer_id={opp['customer_id']}")
            if merchant_exists is None:
                missing.append(f"merchant_id={opp['merchant_id']}")
            reason = f"Dangling reference on opportunity {opp['opportunity_id']}: {', '.join(missing)} not found."
            _log_data_integrity_failure(conn, opp["opportunity_id"], reason)
            raise DataIntegrityError(reason)

        risk_event = conn.execute(
            """
            SELECT risk_event_id, order_id, subscription_id, invoice_id,
                   event_type, root_cause, amount_at_risk
            FROM revenue_risk_events
            WHERE risk_event_id = ?
            """,
            (opp["revenue_risk_event_id"],),
        ).fetchone()

        currency = "INR"
        payment_failure_code = None
        payment_method = None
        if risk_event["order_id"]:
            order = conn.execute(
                "SELECT currency FROM orders WHERE order_id = ?",
                (risk_event["order_id"],),
            ).fetchone()
            if order is not None and order["currency"]:
                currency = order["currency"]
            pa = conn.execute(
                """
                SELECT failure_code, payment_method
                FROM payment_attempts
                WHERE order_id = ?
                ORDER BY attempted_at DESC
                LIMIT 1
                """,
                (risk_event["order_id"],),
            ).fetchone()
            if pa is not None:
                payment_failure_code = pa["failure_code"]
                payment_method = pa["payment_method"]

        # Mandate reauth flag, if this event is subscription-linked.
        mandate_requires_reauth = False
        if risk_event["subscription_id"]:
            mandate = conn.execute(
                """
                SELECT requires_reauth
                FROM mandates
                WHERE subscription_id = ?
                """,
                (risk_event["subscription_id"],),
            ).fetchone()
            if mandate is not None:
                mandate_requires_reauth = str(mandate["requires_reauth"]) in ("1", "true", "True")

        return {
            "risk_event_id": risk_event["risk_event_id"],
            "order_id": risk_event["order_id"],
            "subscription_id": risk_event["subscription_id"],
            "invoice_id": risk_event["invoice_id"],
            "customer_id": opp["customer_id"],
            "merchant_id": opp["merchant_id"],
            "amount": risk_event["amount_at_risk"],
            "currency": currency,
            "event_type": risk_event["event_type"],
            "risk_root_cause": risk_event["root_cause"],
            "payment_failure_code": payment_failure_code,
            "payment_method": payment_method,
            "mandate_requires_reauth": mandate_requires_reauth,
            "recommended_intervention": opp["recommended_intervention"],
            "estimated_recovery_probability": opp["estimated_recovery_probability"],
            # If upstream already marked this ineligible, short-circuit later
            # nodes by pre-setting policy fields — the guardrail node still
            # runs, but this makes the reason traceable to its true source.
            "policy_rejection_reason": (
                f"pre-ineligible: {opp['ineligible_reason']}"
                if str(opp["eligible_for_contact"]) in ("0", "false", "False")
                # A recovery webhook (payment.captured / payment_link.paid /
                # subscription.charged) may close the loop while this run was
                # still queued — never message a customer who already paid.
                else "opportunity already recovered"
                if opp["status"] == "recovered" else None
            ),
        }
    finally:
        conn.close()


def triage_root_cause_node(state: RecoveryState) -> Dict[str, Any]:
    """Classifies into an actionable category.

    Prefers the granular payment_attempts.failure_code taxonomy; falls back
    to revenue_risk_events.root_cause when there's no payment attempt
    (e.g. a subscription/mandate-only event with no card/UPI attempt logged).
    """
    code = (state.get("payment_failure_code") or "").lower()
    risk_cause = (state.get("risk_root_cause") or "").lower()
    signal = code or risk_cause

    if state.get("mandate_requires_reauth"):
        category = "mandate_reauth_required"
    elif any(k in signal for k in ("timeout", "npci", "bank_server_error", "bank_outage")):
        category = "network_outage"
    elif "insufficient_funds" in signal:
        category = "insufficient_balance"
    elif any(k in signal for k in ("expired_card", "card_declined", "incorrect_pin")):
        category = "auth_failure"
    elif "customer_cancelled" in signal:
        category = "customer_intent_cancel"
    else:
        category = "unknown_dropoff"

    return {"root_cause_category": category}


def compliance_guardrail_node(state: RecoveryState) -> Dict[str, Any]:
    """Enforces the merchant's actual contact_policies + real contact history.

    Checks, in order:
      0. Pre-ineligible from recovery_opportunities (set during ingestion)
      1. Opt-out on file for this customer
      2. Quiet hours (merchant-specific, from contact_policies)
      3. Contacts in the last 24h / 7d vs. the merchant's caps
      4. Minimum interval since the last contact
    Any prior "no_action" business decision (e.g. low recovery probability
    driving recommended_intervention == 'no_action') is treated as a policy
    pass-through — that's a strategy decision, not a compliance block — so
    it's left to strategy_selector, not this node.
    """
    if state.get("policy_rejection_reason"):
        # Already ineligible per recovery_opportunities.
        return {"policy_approved": False, "execution_status": "skipped"}

    conn = get_conn()
    try:
        policy = conn.execute(
            """
            SELECT max_contacts_per_24h, max_contacts_per_7d,
                   minimum_contact_interval_hours, quiet_hours_start,
                   quiet_hours_end, stop_after_opt_out
            FROM contact_policies
            WHERE merchant_id = ?
            """,
            (state["merchant_id"],),
        ).fetchone()

        if policy is None:
            # No policy on file — fail safe: block and flag for review
            # rather than silently defaulting to "anything goes".
            return {
                "policy_approved": False,
                "policy_rejection_reason": "no contact_policies row for merchant",
                "execution_status": "skipped",
            }

        # Opt-out check: the customer's CURRENT opt state is the latest
        # opt_out/opt_in contact-history row (or any legacy row with the
        # flag set), newest first — so a STOP blocks every future contact
        # and a later START re-enables it. CSV exports may store the flag
        # as TEXT, so compare normalized.
        if str(policy["stop_after_opt_out"]) in ("1", "true", "True"):
            opt_rows = conn.execute(
                """
                SELECT opt_out_flag FROM customer_contact_history
                WHERE customer_id = ?
                  AND (message_status IN ('opt_out', 'opt_in')
                       OR opt_out_flag IN ('1', 'true', 'True', 1))
                ORDER BY contacted_at DESC
                """,
                (state["customer_id"],),
            ).fetchall()
            opted_out = bool(opt_rows) and str(opt_rows[0]["opt_out_flag"]).strip().lower() in ("1", "true")
            if opted_out:
                return {
                    "policy_approved": False,
                    "policy_rejection_reason": "customer opted out",
                    "execution_status": "skipped",
                }

        # Quiet hours check (merchant-specific, e.g. "21:00"/"09:00").
        # contact_policies times are IST wall-clock hours — must compare
        # against IST "now", not whatever timezone the host machine/server
        # happens to be running in (commonly UTC in production).
        now_ist = datetime.now(IST)
        current_hour = now_ist.hour
        qh_start = int(str(policy["quiet_hours_start"]).split(":")[0])
        qh_end = int(str(policy["quiet_hours_end"]).split(":")[0])
        in_quiet_hours = (
            current_hour >= qh_start or current_hour < qh_end
            if qh_start > qh_end
            else qh_start <= current_hour < qh_end
        )
        if in_quiet_hours:
            return {
                "policy_approved": False,
                "policy_rejection_reason": (
                    f"quiet hours active ({policy['quiet_hours_start']}-{policy['quiet_hours_end']})"
                ),
                "execution_status": "skipped",
            }

        # Frequency caps. Cast defensively — some CSV-originated SQLite
        # exports store numeric columns as TEXT rather than INTEGER/REAL.
        # Inbound rows (a customer's reply, STOP or START) are not merchant
        # contacts and never consume the contact budget.
        max_24h = int(float(policy["max_contacts_per_24h"]))
        max_7d = int(float(policy["max_contacts_per_7d"]))
        min_interval_hours = float(policy["minimum_contact_interval_hours"])

        count_24h = conn.execute(
            """
            SELECT COUNT(*) AS c FROM customer_contact_history
            WHERE customer_id = ? AND contacted_at >= datetime('now', '-1 day')
              AND (message_status IS NULL OR message_status NOT IN ('replied', 'opt_out', 'opt_in'))
            """,
            (state["customer_id"],),
        ).fetchone()["c"]
        if count_24h >= max_24h:
            return {
                "policy_approved": False,
                "policy_rejection_reason": f"24h contact cap reached ({count_24h}/{max_24h})",
                "execution_status": "skipped",
            }

        count_7d = conn.execute(
            """
            SELECT COUNT(*) AS c FROM customer_contact_history
            WHERE customer_id = ? AND contacted_at >= datetime('now', '-7 days')
              AND (message_status IS NULL OR message_status NOT IN ('replied', 'opt_out', 'opt_in'))
            """,
            (state["customer_id"],),
        ).fetchone()["c"]
        if count_7d >= max_7d:
            return {
                "policy_approved": False,
                "policy_rejection_reason": f"7d contact cap reached ({count_7d}/{max_7d})",
                "execution_status": "skipped",
            }

        # Minimum interval since last contact (same inbound exclusion)
        last_contact = conn.execute(
            """
            SELECT contacted_at FROM customer_contact_history
            WHERE customer_id = ?
              AND (message_status IS NULL OR message_status NOT IN ('replied', 'opt_out', 'opt_in'))
            ORDER BY contacted_at DESC LIMIT 1
            """,
            (state["customer_id"],),
        ).fetchone()
        if last_contact is not None:
            try:
                last_dt = datetime.fromisoformat(
                    str(last_contact["contacted_at"]).replace("Z", "+00:00").replace(" ", "T")
                )
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            except ValueError:
                hours_since = float("inf")  # unparseable timestamp: don't block on it
            if hours_since < min_interval_hours:
                return {
                    "policy_approved": False,
                    "policy_rejection_reason": (
                        f"minimum contact interval not met "
                        f"({hours_since:.1f}h < {min_interval_hours}h)"
                    ),
                    "execution_status": "skipped",
                }

        return {"policy_approved": True, "policy_rejection_reason": None}
    finally:
        conn.close()


def strategy_selector_node(state: RecoveryState) -> Dict[str, Any]:
    """Selects a recovery strategy + channel. Mandate reauth takes priority
    over generic auth-failure routing."""
    category = state.get("root_cause_category")

    if category == "mandate_reauth_required":
        strategy, channel, confidence = "request_mandate_reauth", "whatsapp", 0.55
    elif category == "network_outage":
        strategy, channel, confidence = "delayed_smart_retry", "whatsapp", 0.6
    elif category == "insufficient_balance":
        strategy, channel, confidence = "offer_bnpl_or_split_pay", "whatsapp", 0.45
    elif category == "auth_failure":
        strategy, channel, confidence = "send_one_click_retry_link", "sms", 0.5
    elif category == "customer_intent_cancel":
        strategy, channel, confidence = "no_action", "none", 0.1
    else:
        strategy, channel, confidence = "standard_dunning_reminder", "email", 0.3

    # Human-in-the-loop: a policy-approved but low-confidence decision is
    # parked for a person instead of auto-dispatching. Policy-blocked
    # decisions never need review — the guardrail already said no.
    requires_review = bool(
        state.get("policy_approved")
        and confidence is not None
        and confidence < settings.human_review_confidence_threshold
        and strategy != "no_action"
    )

    return {
        "selected_strategy": strategy,
        "chosen_channel": channel,
        "confidence_score": confidence,
        "requires_human_review": requires_review,
    }


# --- MESSAGE GENERATION ---

class GeneratedMessage(BaseModel):
    """Structured output schema — forces the model to return exactly this
    shape, no preamble, no "Here's your message:" text to strip out."""
    message_body: str = Field(
        description="The exact SMS/WhatsApp copy to send to the customer. "
        "No greeting boilerplate beyond what a real message would have. "
        "Keep it under 300 characters — this is what actually gets sent."
    )
    personalization_reasoning: str = Field(
        description="One or two sentences explaining what customer history "
        "or context drove the specific wording choices — for audit review, "
        "not shown to the customer."
    )


# The LLM chain is built lazily on first use: constructing it at import time
# would crash any environment that doesn't have the langchain-google-genai
# dependency or GOOGLE_API_KEY installed yet, including plain DRY_RUN demos.
_message_chain = None


def _get_message_chain():
    global _message_chain
    if _message_chain is None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(model=settings.gemini_model, temperature=0.2)
        _message_chain = llm.with_structured_output(GeneratedMessage)
    return _message_chain


def _template_message(
    *,
    merchant_name: str,
    amount: Optional[float],
    failure_reason: str,
    payment_link_url: Optional[str],
    channel: Optional[str],
) -> str:
    """Deterministic fallback copy used when Gemini isn't configured or the
    call fails. Marked '[template]' everywhere it's persisted so nobody
    mistakes it for model output."""
    friendly = _FRIENDLY_FAILURE.get(failure_reason, "the payment didn't complete")
    amount_txt = f" of ₹{float(amount):,.2f}" if amount is not None else ""
    link_txt = f" You can complete it securely here: {payment_link_url}" if payment_link_url else ""
    return (
        f"Hi, your payment{amount_txt} to {merchant_name} didn't go through — "
        f"{friendly}.{link_txt} Reply STOP to opt out."
    )


def _load_customer(conn: sqlite3.Connection, customer_id: str):
    return conn.execute(
        "SELECT customer_id, first_name, last_name, email, phone FROM customers WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()


def _ensure_payment_link(state: RecoveryState, merchant_name: str, customer) -> Dict[str, Any]:
    """Creates (once) the Razorpay Payment Link for strategies whose message
    carries a one-click retry. In DRY_RUN this returns a clearly-marked
    placeholder URL and logs the exact payload that would be created."""
    if state.get("payment_link_url"):
        return {}
    if state.get("selected_strategy") not in LINK_STRATEGIES:
        return {}

    result = create_payment_link(
        amount_inr=float(state.get("amount") or 0),
        currency=state.get("currency") or "INR",
        reference_id=state["recovery_opportunity_id"],
        description=f"Payment retry for {merchant_name}",
        customer_name=(
            f"{customer['first_name']} {customer['last_name']}".strip() if customer else None
        ),
        customer_email=customer["email"] if customer else None,
        customer_contact=customer["phone"] if customer else None,
    )
    if result.status == "failed":
        # A missing link must not kill the pipeline — the message goes out
        # without it and the failure is recorded on the dispatch audit row.
        logger.warning("Payment link creation failed for %s: %s",
                       state["recovery_opportunity_id"], result.error)
        return {}
    return {"payment_link_url": result.payment_link_url, "payment_link_id": result.payment_link_id}


def generate_message_node(state: RecoveryState) -> Dict[str, Any]:
    """Drafts the recovery message, with the payment link created FIRST so
    the copy can carry it. Gemini when configured; deterministic template
    otherwise. Only runs on the policy-approved path (see message_gen_router).
    """
    conn = get_conn()
    try:
        history = conn.execute(
            """
            SELECT total_orders, successful_orders, failed_orders, total_spend,
                   average_order_value, historical_payment_success_rate,
                   typical_payment_method, typical_payment_hour_ist
            FROM customer_payment_history WHERE customer_id = ?
            """,
            (state["customer_id"],),
        ).fetchone()

        merchant = conn.execute(
            "SELECT merchant_name, business_type, industry FROM merchants WHERE merchant_id = ?",
            (state["merchant_id"],),
        ).fetchone()

        customer = _load_customer(conn, state["customer_id"])
    finally:
        conn.close()

    merchant_name = merchant["merchant_name"] if merchant else "Unknown merchant"
    updates: Dict[str, Any] = _ensure_payment_link(state, merchant_name, customer)
    payment_link_url = updates.get("payment_link_url") or state.get("payment_link_url")

    tone = derive_merchant_tone(
        merchant["business_type"] if merchant else None,
        merchant["industry"] if merchant else None,
    )
    failure_reason = state.get("payment_failure_code") or state.get("risk_root_cause") or "unknown"

    body: str
    reasoning: str
    model_used: str

    if not settings.google_api_key:
        body = _template_message(
            merchant_name=merchant_name,
            amount=state.get("amount"),
            failure_reason=failure_reason,
            payment_link_url=payment_link_url,
            channel=state.get("chosen_channel"),
        )
        reasoning = "[template] GOOGLE_API_KEY not configured; deterministic fallback copy used."
        model_used = "template-fallback"
    else:
        history_summary = (
            "no prior order history on file" if history is None else
            f"{history['total_orders']} total orders, {history['successful_orders']} successful, "
            f"{history['failed_orders']} failed, historical success rate "
            f"{float(history['historical_payment_success_rate']):.0%}, "
            f"typically pays via {history['typical_payment_method']} around "
            f"{history['typical_payment_hour_ist']}:00 IST, average order value "
            f"₹{float(history['average_order_value']):.2f}"
        )
        link_instruction = (
            f"Include this exact one-click payment link verbatim in the message: {payment_link_url}"
            if payment_link_url
            else "No payment link is available; ask the customer to retry from the app or website."
        )
        prompt = f"""Draft a {state['chosen_channel']} recovery message for a customer whose
payment just failed.

Merchant: {merchant_name}
Merchant tone to write in: {tone}
Customer payment history: {history_summary}
Payment failure reason: {failure_reason}
Chosen recovery strategy: {state.get('selected_strategy')}
Amount at risk: ₹{state.get('amount')}
{link_instruction}

Write ONE message in the merchant's tone that fits the chosen strategy and
acknowledges the specific failure reason without sounding robotic or
repeating the raw error code to the customer."""
        try:
            result: GeneratedMessage = _get_message_chain().invoke(prompt)
            body = result.message_body
            reasoning = result.personalization_reasoning
            model_used = settings.gemini_model
        except Exception as e:
            logger.error("Gemini drafting failed (%s); using template fallback.", e)
            body = _template_message(
                merchant_name=merchant_name,
                amount=state.get("amount"),
                failure_reason=failure_reason,
                payment_link_url=payment_link_url,
                channel=state.get("chosen_channel"),
            )
            reasoning = f"[template] Gemini call failed ({type(e).__name__}: {e}); fallback copy used."
            model_used = "template-fallback"

    updates.update(
        {
            "generated_message_body": body,
            "generated_message_reasoning": reasoning,
            "tone_used": tone,
            "message_model_used": model_used,
        }
    )
    return updates


def persist_decision_node(state: RecoveryState) -> Dict[str, Any]:
    """Writes a recovery_agent_decisions row for EVERY path (approved or
    blocked). This runs regardless of guardrail outcome, so nothing is
    lost from the audit trail — only recovery_agent_actions differs
    downstream based on whether we actually dispatch.

    Idempotent: if a decision already exists for this opportunity — e.g.
    the graph is re-run on the same opportunity, a webhook is delivered
    twice, or a crash forced a retry — reuse it instead of inserting a
    duplicate. A UNIQUE index on recovery_opportunity_id (created by
    db.ensure_schema when the data allows) makes the database itself the
    final backstop against the check-then-insert race.
    """
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT decision_id FROM recovery_agent_decisions WHERE recovery_opportunity_id = ?",
            (state["recovery_opportunity_id"],),
        ).fetchone()
        if existing is not None:
            return {"decision_id": existing["decision_id"]}

        decision_id = _new_id()
        approved = state.get("policy_approved", False)
        reason = state.get("policy_rejection_reason")
        requires_review = 1 if state.get("requires_human_review") else 0

        reasoning = (
            f"root_cause={state.get('root_cause_category')}; "
            f"strategy={state.get('selected_strategy')}; "
            f"policy_check={'passed' if approved else 'blocked: ' + str(reason)}"
        )
        if approved and requires_review:
            reasoning += (
                f"; held for human review (confidence "
                f"{state.get('confidence_score')} < "
                f"{settings.human_review_confidence_threshold})"
            )

        try:
            conn.execute(
                """
                INSERT INTO recovery_agent_decisions (
                    decision_id, recovery_opportunity_id, risk_assessment,
                    estimated_recovery_probability, recommended_action,
                    confidence_score, reasoning_summary, policy_check_result,
                    requires_human_review, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    state["recovery_opportunity_id"],
                    state.get("root_cause_category"),
                    state.get("estimated_recovery_probability"),
                    state.get("selected_strategy") or "no_action",
                    state.get("confidence_score"),
                    reasoning,
                    "passed" if approved else "blocked",
                    requires_review,
                    _now_iso(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost the race against a concurrent run of the same opportunity
            # (e.g. webhook + worker): reuse the winner's decision.
            conn.rollback()
            winner = conn.execute(
                "SELECT decision_id FROM recovery_agent_decisions WHERE recovery_opportunity_id = ?",
                (state["recovery_opportunity_id"],),
            ).fetchone()
            return {"decision_id": winner["decision_id"] if winner else None}

        return {"decision_id": decision_id}
    finally:
        conn.close()


def persist_message(state: RecoveryState, conn: sqlite3.Connection) -> Optional[str]:
    """Writes the drafted message (with its payment link) once per decision.
    Runs for auto-dispatch AND for pending_review drafts so the reviewer can
    see exactly what would go out."""
    if not state.get("generated_message_body") or not state.get("decision_id"):
        return None
    existing = conn.execute(
        "SELECT message_id FROM recovery_generated_messages WHERE decision_id = ?",
        (state["decision_id"],),
    ).fetchone()
    if existing is not None:
        return existing["message_id"]
    message_id = _new_id()
    conn.execute(
        """
        INSERT INTO recovery_generated_messages (
            message_id, decision_id, recovery_opportunity_id, channel,
            message_body, tone_used, personalization_reasoning,
            model_used, payment_link_url, generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            state["decision_id"],
            state["recovery_opportunity_id"],
            state.get("chosen_channel"),
            state.get("generated_message_body"),
            state.get("tone_used"),
            state.get("generated_message_reasoning"),
            state.get("message_model_used"),
            state.get("payment_link_url"),
            _now_iso(),
        ),
    )
    return message_id


def _record_dispatch(conn: sqlite3.Connection, *, state: RecoveryState, result) -> str:
    dispatch_id = _new_id()
    conn.execute(
        """
        INSERT INTO outbound_dispatches (
            dispatch_id, action_id, decision_id, recovery_opportunity_id,
            channel, provider, provider_message_id, payment_link_url,
            payment_link_id, dry_run, status, payload_summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dispatch_id,
            state.get("action_id"),
            state["decision_id"],
            state["recovery_opportunity_id"],
            result.channel,
            result.provider,
            result.provider_message_id,
            result.payment_link_url or state.get("payment_link_url"),
            result.payment_link_id or state.get("payment_link_id"),
            1 if result.dry_run else 0,
            result.status,
            result.payload_summary,
            _now_iso(),
        ),
    )
    return dispatch_id


def dispatch_action_node(state: RecoveryState) -> Dict[str, Any]:
    """Executes, parks for review, or records as skipped the chosen action.

    Three paths:
      - policy blocked  -> action 'skipped' (+ blocked recovery_attempts row)
      - approved but low confidence -> action 'pending_review', draft message
        persisted, NO customer contact; a human resolves it from the review
        queue via execute_human_approved_dispatch / human_reject_decision
      - approved -> send via dispatchers (Twilio/Resend; DRY_RUN logs the
        exact payload), then write recovery_agent_actions ('success' or
        'failed'), recovery_interventions, customer_contact_history (so the
        guardrail sees its own sends), recovery_attempts and
        outbound_dispatches together.

    Idempotent: if an action already exists for this decision_id, reuse the
    prior outcome instead of dispatching/logging again.
    """
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT action_id, status FROM recovery_agent_actions WHERE decision_id = ?",
            (state["decision_id"],),
        ).fetchone()
        if existing is not None:
            return {"action_id": existing["action_id"], "execution_status": existing["status"]}

        action_id = _new_id()
        approved = state.get("policy_approved", False)
        requires_review = bool(state.get("requires_human_review"))
        started = datetime.now(timezone.utc)

        if not approved:
            conn.execute(
                """
                INSERT INTO recovery_agent_actions (
                    action_id, decision_id, action_type, status,
                    failure_reason, amount_affected, execution_latency_ms, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    state["decision_id"],
                    state.get("selected_strategy") or "no_action",
                    "skipped",
                    state.get("policy_rejection_reason"),
                    state.get("amount"),
                    0,
                    _now_iso(),
                ),
            )
            conn.execute(
                """
                INSERT INTO recovery_attempts (
                    recovery_attempt_id, recovery_opportunity_id, payment_attempt_id,
                    contact_history_id, response_id, attempt_type, status,
                    dispatched_at, delivered_at, delivery_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id(),
                    state["recovery_opportunity_id"],
                    None,
                    None,
                    None,
                    state.get("selected_strategy") or "no_action",
                    "blocked",
                    _now_iso(),
                    None,
                    None,
                ),
            )
            conn.commit()
            return {"action_id": action_id, "execution_status": "skipped"}

        if requires_review:
            persist_message(state, conn)
            conn.execute(
                """
                INSERT INTO recovery_agent_actions (
                    action_id, decision_id, action_type, status,
                    failure_reason, amount_affected, execution_latency_ms, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    state["decision_id"],
                    state.get("selected_strategy") or "no_action",
                    "pending_review",
                    None,
                    state.get("amount"),
                    0,
                    _now_iso(),
                ),
            )
            _audit(
                conn,
                decision_id=state["decision_id"],
                action_id=action_id,
                event_type="held_for_human_review",
                before_state=None,
                after_state=f"confidence={state.get('confidence_score')} below "
                            f"threshold={settings.human_review_confidence_threshold}",
            )
            conn.commit()
            return {"action_id": action_id, "execution_status": "pending_review"}

        # Approved: actually send (or DRY-RUN the send). The action row is
        # written as 'processing' and committed BEFORE the external call —
        # a crash mid-dispatch then leaves a durable claim the reconciler
        # (reconcile.py) can replay, instead of an invisible half-send.
        persist_message(state, conn)
        conn.execute(
            """
            INSERT INTO recovery_agent_actions (
                action_id, decision_id, action_type, status,
                failure_reason, amount_affected, execution_latency_ms, executed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                state["decision_id"],
                state.get("selected_strategy") or "no_action",
                "processing",
                None,
                state.get("amount"),
                0,
                _now_iso(),
            ),
        )
        conn.commit()

        customer = _load_customer(conn, state["customer_id"])
        result = send_message(
            channel=state.get("chosen_channel") or "none",
            body=state.get("generated_message_body") or "",
            to_phone=customer["phone"] if customer else None,
            to_email=customer["email"] if customer else None,
            subject=f"Complete your payment — action needed",
        )
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        send_ok = result.status in ("sent", "dry_run", "skipped")
        action_status = "success" if send_ok else "failed"

        conn.execute(
            "UPDATE recovery_agent_actions SET status = ?, failure_reason = ?, "
            "execution_latency_ms = ?, executed_at = ? WHERE action_id = ?",
            (action_status, None if send_ok else result.error, latency_ms, _now_iso(), action_id),
        )

        contact_history_id = None
        if send_ok and result.channel != "none":
            executed_at = _now_iso()
            intervention_id = _new_id()
            conn.execute(
                """
                INSERT INTO recovery_interventions (
                    intervention_id, recovery_opportunity_id, channel,
                    scheduled_at, executed_at, status, blocked_reason,
                    message_template, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intervention_id,
                    state["recovery_opportunity_id"],
                    state.get("chosen_channel"),
                    executed_at,
                    executed_at,
                    "executed",
                    None,
                    "llm_generated_v1",
                    executed_at,
                ),
            )

            contact_history_id = _new_id()
            conn.execute(
                """
                INSERT INTO customer_contact_history (
                    contact_history_id, customer_id, intervention_id,
                    channel, message_status, opt_out_flag, contacted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact_history_id,
                    state["customer_id"],
                    intervention_id,
                    state.get("chosen_channel"),
                    "sent",
                    0,
                    executed_at,
                ),
            )

            conn.execute(
                """
                INSERT INTO recovery_attempts (
                    recovery_attempt_id, recovery_opportunity_id, payment_attempt_id,
                    contact_history_id, response_id, attempt_type, status,
                    dispatched_at, delivered_at, delivery_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id(),
                    state["recovery_opportunity_id"],
                    None,
                    contact_history_id,
                    None,
                    state.get("selected_strategy") or "no_action",
                    "success",
                    executed_at,
                    None,
                    None,
                ),
            )

        state_updates = {"action_id": action_id}
        _record_dispatch(conn, state={**state, "action_id": action_id}, result=result)
        conn.commit()
        state_updates["execution_status"] = "dispatched" if send_ok else "failed"
        return state_updates
    finally:
        conn.close()


# --- HUMAN-IN-THE-LOOP RESOLUTION (called by main.py's approve/reject) ---

def claim_decision_action(conn, decision) -> str | None:
    """Atomically claim a decision for dispatch and return the action_id.

    The claim is a committed recovery_agent_actions row in 'processing'
    state; the UNIQUE index on decision_id plus the guarded UPDATE make it
    safe against concurrent claimants. Returns None when the decision was
    already dispatched or is claimed elsewhere."""
    existing = conn.execute(
        "SELECT action_id, status FROM recovery_agent_actions WHERE decision_id = ?",
        (decision["decision_id"],),
    ).fetchone()
    if existing is None:
        action_id = _new_id()
        try:
            conn.execute(
                """
                INSERT INTO recovery_agent_actions (
                    action_id, decision_id, action_type, status,
                    failure_reason, amount_affected, execution_latency_ms, executed_at
                ) VALUES (?, ?, ?, 'processing', NULL, NULL, 0, ?)
                """,
                (action_id, decision["decision_id"],
                 decision["recommended_action"] or "no_action", _now_iso()),
            )
        except sqlite3.IntegrityError:
            return None  # concurrent claimant won the insert race
        return action_id
    if existing["status"] == "success":
        return None
    cur = conn.execute(
        "UPDATE recovery_agent_actions SET status = 'processing', executed_at = ? "
        "WHERE action_id = ? AND status IN ('pending_review', 'processing', 'failed')",
        (_now_iso(), existing["action_id"]),
    )
    if cur.rowcount == 0:
        return None
    return existing["action_id"]


def dispatch_claimed_decision(decision_id: str, action_id: str,
                              audit_action: str = "human_approved_dispatch") -> Dict[str, Any]:
    """Sends (or dry-run sends) for a decision whose action row the caller
    already holds a 'processing' claim on. Used by the human-approve flow
    and by reconcile.reconcile_pending() when it replays a dispatch that was
    interrupted between claim and final write.

    Reuses the parked draft (including its payment link); drafts one on the
    spot if the decision predates message persistence."""
    conn = get_conn()
    try:
        decision = conn.execute(
            "SELECT * FROM recovery_agent_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if decision is None:
            raise ValueError(f"No decision found with id {decision_id}")

        opp = conn.execute(
            """
            SELECT ro.*, rre.amount_at_risk, rre.order_id, rre.root_cause
            FROM recovery_opportunities ro
            JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id
            WHERE ro.opportunity_id = ?
            """,
            (decision["recovery_opportunity_id"],),
        ).fetchone()
        if opp is None:
            raise ValueError("Backing opportunity no longer exists.")

        customer = _load_customer(conn, opp["customer_id"])
        message = conn.execute(
            "SELECT * FROM recovery_generated_messages WHERE decision_id = ?", (decision_id,)
        ).fetchone()

        # Strategy/channel come from stored rows — the graph is not re-run,
        # so nothing that was already reviewed can change under the sender.
        strategy = decision["recommended_action"]
        channel = (message["channel"] if message else None) or "whatsapp"

        payment_link_url = message["payment_link_url"] if message else None
        payment_link_id = None
        if strategy in LINK_STRATEGIES and not payment_link_url:
            merchant = conn.execute(
                "SELECT merchant_name FROM merchants WHERE merchant_id = ?", (opp["merchant_id"],)
            ).fetchone()
            link = create_payment_link(
                amount_inr=float(opp["amount_at_risk"] or 0),
                currency="INR",
                reference_id=opp["opportunity_id"],
                description=f"Payment retry for {merchant['merchant_name'] if merchant else 'merchant'}",
                customer_name=(
                    f"{customer['first_name']} {customer['last_name']}".strip() if customer else None
                ),
                customer_email=customer["email"] if customer else None,
                customer_contact=customer["phone"] if customer else None,
            )
            if link.status != "failed":
                payment_link_url = link.payment_link_url
                payment_link_id = link.payment_link_id

        body = message["message_body"] if message else None
        if not body:
            body = _template_message(
                merchant_name="the merchant",
                amount=opp["amount_at_risk"],
                failure_reason=opp["root_cause"] or "unknown",
                payment_link_url=payment_link_url,
                channel=channel,
            )
            conn.execute(
                """
                INSERT INTO recovery_generated_messages (
                    message_id, decision_id, recovery_opportunity_id, channel,
                    message_body, tone_used, personalization_reasoning,
                    model_used, payment_link_url, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id(),
                    decision_id,
                    opp["opportunity_id"],
                    channel,
                    body,
                    "[template]",
                    "[template] drafted at dispatch time; no prior draft existed.",
                    "template-fallback",
                    payment_link_url,
                    _now_iso(),
                ),
            )
        elif payment_link_url and payment_link_url not in body:
            body = f"{body} {payment_link_url}"

        started = datetime.now(timezone.utc)
        result = send_message(
            channel=channel,
            body=body,
            to_phone=customer["phone"] if customer else None,
            to_email=customer["email"] if customer else None,
            subject="Complete your payment — action needed",
        )
        send_ok = result.status in ("sent", "dry_run", "skipped")
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        now = _now_iso()

        conn.execute(
            "UPDATE recovery_agent_actions SET status = ?, failure_reason = ?, "
            "amount_affected = ?, execution_latency_ms = ?, executed_at = ? WHERE action_id = ?",
            ("success" if send_ok else "failed", None if send_ok else result.error,
             opp["amount_at_risk"], latency_ms, now, action_id),
        )

        if send_ok and channel != "none":
            intervention_id = _new_id()
            conn.execute(
                """
                INSERT INTO recovery_interventions (
                    intervention_id, recovery_opportunity_id, channel,
                    scheduled_at, executed_at, status, blocked_reason,
                    message_template, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (intervention_id, opp["opportunity_id"], channel, now, now,
                 "executed", None, "llm_generated_v1", now),
            )
            contact_history_id = _new_id()
            conn.execute(
                """
                INSERT INTO customer_contact_history (
                    contact_history_id, customer_id, intervention_id,
                    channel, message_status, opt_out_flag, contacted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (contact_history_id, opp["customer_id"], intervention_id, channel, "sent", 0, now),
            )
            conn.execute(
                """
                INSERT INTO recovery_attempts (
                    recovery_attempt_id, recovery_opportunity_id, payment_attempt_id,
                    contact_history_id, response_id, attempt_type, status,
                    dispatched_at, delivered_at, delivery_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_new_id(), opp["opportunity_id"], None, contact_history_id, None,
                 strategy, "success", now, None, None),
            )

        _record_dispatch(
            conn,
            state={
                "action_id": action_id,
                "decision_id": decision_id,
                "recovery_opportunity_id": opp["opportunity_id"],
                "payment_link_url": payment_link_url,
                "payment_link_id": payment_link_id,
            },
            result=result,
        )
        _audit(
            conn,
            decision_id=decision_id,
            action_id=action_id,
            event_type=audit_action,
            before_state="processing",
            after_state="success" if send_ok else f"failed: {result.error}",
        )
        conn.commit()
        return {
            "decision_id": decision_id,
            "status": "dispatched" if send_ok else "failed",
            "dry_run": result.dry_run,
            "channel": channel,
            "provider": result.provider,
        }
    finally:
        conn.close()


def execute_human_approved_dispatch(decision_id: str) -> Dict[str, Any]:
    """Sends the message a human just approved. Claims the decision first;
    the claim is committed before any external call, so a crash mid-send is
    replayable by the reconciler instead of invisible.

    Raises ValueError on a decision that can't be dispatched (missing or
    policy-blocked); returns already_dispatched when another path won."""
    conn = get_conn()
    try:
        decision = conn.execute(
            "SELECT * FROM recovery_agent_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if decision is None:
            raise ValueError(f"No decision found with id {decision_id}")
        if decision["policy_check_result"] != "passed":
            raise ValueError("Decision was policy-blocked; there is nothing safe to send.")
        action_id = claim_decision_action(conn, decision)
        if action_id is None:
            return {"decision_id": decision_id, "status": "already_dispatched"}
        conn.commit()
    finally:
        conn.close()
    return dispatch_claimed_decision(decision_id, action_id)


def human_reject_decision(decision_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Resolves a flagged decision as rejected: no contact ever goes out.
    Updates the parked action to 'skipped' so the opportunity feed shows the
    outcome honestly, and writes the human audit trail."""
    conn = get_conn()
    try:
        decision = conn.execute(
            "SELECT decision_id, recovery_opportunity_id, recommended_action FROM recovery_agent_decisions "
            "WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if decision is None:
            raise ValueError(f"No decision found with id {decision_id}")

        rejection = reason or "rejected by human reviewer"
        now = _now_iso()

        action = conn.execute(
            "SELECT action_id, status FROM recovery_agent_actions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if action is None:
            action_id = _new_id()
            conn.execute(
                """
                INSERT INTO recovery_agent_actions (
                    action_id, decision_id, action_type, status,
                    failure_reason, amount_affected, execution_latency_ms, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (action_id, decision_id, decision["recommended_action"] or "no_action",
                 "skipped", rejection, None, 0, now),
            )
        else:
            action_id = action["action_id"]
            if action["status"] == "success":
                return {"decision_id": decision_id, "status": "already_dispatched"}
            conn.execute(
                "UPDATE recovery_agent_actions SET status = 'skipped', failure_reason = ?, executed_at = ? "
                "WHERE action_id = ?",
                (rejection, now, action_id),
            )

        conn.execute(
            """
            INSERT INTO recovery_attempts (
                recovery_attempt_id, recovery_opportunity_id, payment_attempt_id,
                contact_history_id, response_id, attempt_type, status,
                dispatched_at, delivered_at, delivery_latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_new_id(), decision["recovery_opportunity_id"], None, None, None,
             decision["recommended_action"] or "no_action", "rejected", now, None, None),
        )
        _audit(
            conn,
            decision_id=decision_id,
            action_id=action_id,
            event_type="human_rejected",
            before_state="pending_review",
            after_state=rejection,
        )
        conn.commit()
        return {"decision_id": decision_id, "status": "rejected"}
    finally:
        conn.close()


# --- 3. CONDITIONAL ROUTERS ---

def message_gen_router(state: RecoveryState) -> str:
    """Only draft a message when the guardrail actually approved contact —
    no point spending an LLM call on a customer we're not messaging."""
    return "generate_message" if state.get("policy_approved") else "persist_decision"


def guardrail_router(state: RecoveryState) -> str:
    """After persisting the decision (which always happens), route to real
    dispatch either way — dispatch_action_node itself decides
    success vs. pending_review vs. skipped so every path still gets logged."""
    return "dispatch"


# --- 4. GRAPH ASSEMBLY ---

workflow = StateGraph(RecoveryState)

workflow.add_node("ingest", risk_event_ingestion_node)
workflow.add_node("triage", triage_root_cause_node)
workflow.add_node("guardrail", compliance_guardrail_node)
workflow.add_node("strategy_selector", strategy_selector_node)
workflow.add_node("generate_message", generate_message_node)
workflow.add_node("persist_decision", persist_decision_node)
workflow.add_node("dispatch", dispatch_action_node)

workflow.set_entry_point("ingest")
workflow.add_edge("ingest", "triage")
workflow.add_edge("triage", "guardrail")
# Strategy is computed regardless of guardrail outcome — a blocked
# opportunity still needs a `selected_strategy` value recorded on its
# decision row (what *would* have been done), matching
# recovery_ground_truth.intervention_that_would_have_worked in your schema.
workflow.add_edge("guardrail", "strategy_selector")
# Message generation only happens on the approved path — see message_gen_router.
workflow.add_conditional_edges(
    "strategy_selector", message_gen_router,
    {"generate_message": "generate_message", "persist_decision": "persist_decision"},
)
workflow.add_edge("generate_message", "persist_decision")
workflow.add_conditional_edges("persist_decision", guardrail_router, {"dispatch": "dispatch"})
workflow.add_edge("dispatch", END)

recovery_app = workflow.compile()


def run_recovery_for_opportunity(recovery_opportunity_id: str,
                                 trigger_source: str = "worker") -> Dict[str, Any]:
    """Runs the full graph for one opportunity. This is what the webhook
    handler and the background worker both call — the agent is triggered
    the moment a failure event lands, not only on the next poll cycle.

    Every run is traced in the agent_runs table: trigger, start/finish,
    final execution status, chosen strategy, per-node completion times, and
    the error text when it raises. The trace row is committed BEFORE the
    graph starts, so even a hard crash leaves evidence the run began.
    """
    import time as _time

    run_id = _new_id()
    started_at = _now_iso()
    t0 = _time.monotonic()
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, recovery_opportunity_id, trigger_source,
                started_at, finished_at, status,
                selected_strategy, execution_status, error, node_trace_json
            ) VALUES (?, ?, ?, ?, NULL, 'running', NULL, NULL, NULL, NULL)
            """,
            (run_id, recovery_opportunity_id, trigger_source, started_at),
        )
        conn.commit()
    finally:
        conn.close()

    node_trace: list = []
    try:
        # stream() yields {node_name: state_update} as each node completes,
        # giving per-node timing; accumulating the updates reproduces
        # invoke()'s final state.
        state: Dict[str, Any] = {"recovery_opportunity_id": recovery_opportunity_id}
        for chunk in recovery_app.stream(state):
            for node_name, update in chunk.items():
                node_trace.append({
                    "node": node_name,
                    "t_ms": int((_time.monotonic() - t0) * 1000),
                })
                if isinstance(update, dict):
                    state.update(update)
        result = state
        status, error = "completed", None
    except Exception as e:
        status, error = "error", f"{type(e).__name__}: {e}"[:500]
        result = None
        raised = e

    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE agent_runs
            SET finished_at = ?, status = ?, selected_strategy = ?,
                execution_status = ?, error = ?, node_trace_json = ?
            WHERE run_id = ?
            """,
            (
                _now_iso(), status,
                (result or {}).get("selected_strategy"),
                (result or {}).get("execution_status"),
                error,
                json.dumps(node_trace),
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if result is None:
        raise raised  # re-raise the original exception after recording it
    result["agent_run_id"] = run_id
    return result

def run_batch(limit: Optional[int] = None) -> Dict[str, int]:
    """Processes every currently-undecided recovery_opportunities row (or up
    to `limit` of them), one at a time. Each opportunity is isolated: if it
    raises (DataIntegrityError, or anything else — a DB hiccup, a bad row),
    that failure is caught, logged, and the batch moves on to the next
    opportunity. This is the realistic shape for how the agent runs in
    production — new opportunities arrive continuously, and one bad record
    should never stop the rest of the queue from being processed.

    Returns a summary dict: {"dispatched": n, "skipped": n, "errors": n, ...}.
    """
    conn = get_conn()
    query = """
        SELECT ro.opportunity_id
        FROM recovery_opportunities ro
        LEFT JOIN recovery_agent_decisions rad
            ON rad.recovery_opportunity_id = ro.opportunity_id
        WHERE rad.decision_id IS NULL
          AND (ro.status IS NULL OR ro.status != 'recovered')
    """
    if limit is not None:
        query += " LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()
    conn.close()

    opportunity_ids = [r["opportunity_id"] for r in rows]
    summary: Dict[str, int] = {"dispatched": 0, "skipped": 0, "pending_review": 0, "errors": 0}

    if not opportunity_ids:
        print("No undecided opportunities to process.")
        return summary

    print(f"Processing {len(opportunity_ids)} undecided opportunit{'y' if len(opportunity_ids) == 1 else 'ies'}...\n")

    for i, opportunity_id in enumerate(opportunity_ids, start=1):
        try:
            result = run_recovery_for_opportunity(opportunity_id)
            status = result.get("execution_status", "unknown")
            summary[status] = summary.get(status, 0) + 1
            print(f"[{i}/{len(opportunity_ids)}] {opportunity_id} -> {status} "
                  f"(strategy={result.get('selected_strategy')}, "
                  f"reason={result.get('policy_rejection_reason')})")
        except DataIntegrityError as e:
            summary["errors"] += 1
            print(f"[{i}/{len(opportunity_ids)}] {opportunity_id} -> DATA INTEGRITY ERROR: {e}")
        except Exception as e:
            # Catch-all is intentional here: a single malformed row (a
            # missing FK we didn't anticipate, a malformed timestamp, a
            # locked DB) must not take down the rest of the batch. It's
            # still printed loudly and counted, not swallowed.
            summary["errors"] += 1
            print(f"[{i}/{len(opportunity_ids)}] {opportunity_id} -> UNEXPECTED ERROR: {type(e).__name__}: {e}")

    print(f"\nDone. dispatched={summary.get('dispatched', 0)} "
          f"skipped={summary.get('skipped', 0)} "
          f"pending_review={summary.get('pending_review', 0)} errors={summary['errors']}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the recovery agent over all currently undecided opportunities.")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of opportunities to process (default: all undecided ones).",
    )
    args = parser.parse_args()

    from db import init_db
    init_db()
    run_batch(limit=args.limit)
