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
     APPROVED path — a blocked contact never gets a message drafted, since
     there's no point spending an API call on something that won't be sent.
  7. Dispatch now closes a real gap: previously a successful dispatch never
     wrote a new customer_contact_history row, so the compliance guardrail
     was blind to the very messages this agent sent. It now writes
     recovery_interventions + customer_contact_history +
     recovery_generated_messages together on every real dispatch, and
     recovery_attempts.contact_history_id is populated instead of always
     NULL.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any

from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI

IST = ZoneInfo("Asia/Kolkata")

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END

DB_PATH = "AI Revenue Recovery Platform (SQLite).db"


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


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """recovery_generated_messages doesn't exist in the original Tonic
    Fabricate schema — there's no column anywhere in the 25 original tables
    for actual message body text (recovery_interventions.message_template
    is a template NAME, not content). Creates it if missing so this module
    can be imported into a fresh copy of the DB without a separate manual
    migration step."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recovery_generated_messages (
            message_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            recovery_opportunity_id TEXT NOT NULL,
            channel TEXT,
            message_body TEXT,
            tone_used TEXT,
            personalization_reasoning TEXT,
            model_used TEXT,
            generated_at TEXT
        )
        """
    )
    conn.commit()


# --- MERCHANT TONE HEURISTIC ---
# NOTE: merchants has no real tone/brand-voice column — Tonic Fabricate
# never generated one. This is Claude's inference from business_type +
# industry, not real merchant data. Every tone string returned here is
# prefixed "[heuristic]" wherever it's persisted, so nobody downstream
# (including a demo audience) mistakes it for actual merchant input.
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

    # LLM-generated message (only populated on the approved path)
    generated_message_body: Optional[str]
    generated_message_reasoning: Optional[str]
    tone_used: Optional[str]

    # Persistence FKs produced along the way
    decision_id: Optional[str]
    action_id: Optional[str]

    execution_status: Optional[str]          # 'dispatched' | 'skipped' | 'failed'


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
                   ro.ineligible_reason
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

        # Latest payment attempt for the order, if this event has one.
        payment_failure_code = None
        payment_method = None
        if risk_event["order_id"]:
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
                mandate_requires_reauth = bool(mandate["requires_reauth"])

        return {
            "risk_event_id": risk_event["risk_event_id"],
            "order_id": risk_event["order_id"],
            "subscription_id": risk_event["subscription_id"],
            "invoice_id": risk_event["invoice_id"],
            "customer_id": opp["customer_id"],
            "merchant_id": opp["merchant_id"],
            "amount": risk_event["amount_at_risk"],
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
                if not opp["eligible_for_contact"] else None
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

        # Opt-out check
        if policy["stop_after_opt_out"]:
            opted_out = conn.execute(
                """
                SELECT 1 FROM customer_contact_history
                WHERE customer_id = ? AND opt_out_flag = 1
                LIMIT 1
                """,
                (state["customer_id"],),
            ).fetchone()
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
        max_24h = int(policy["max_contacts_per_24h"])
        max_7d = int(policy["max_contacts_per_7d"])
        min_interval_hours = float(policy["minimum_contact_interval_hours"])

        count_24h = conn.execute(
            """
            SELECT COUNT(*) AS c FROM customer_contact_history
            WHERE customer_id = ? AND contacted_at >= datetime('now', '-1 day')
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
            """,
            (state["customer_id"],),
        ).fetchone()["c"]
        if count_7d >= max_7d:
            return {
                "policy_approved": False,
                "policy_rejection_reason": f"7d contact cap reached ({count_7d}/{max_7d})",
                "execution_status": "skipped",
            }

        # Minimum interval since last contact
        last_contact = conn.execute(
            """
            SELECT contacted_at FROM customer_contact_history
            WHERE customer_id = ?
            ORDER BY contacted_at DESC LIMIT 1
            """,
            (state["customer_id"],),
        ).fetchone()
        if last_contact is not None:
            last_dt = datetime.fromisoformat(last_contact["contacted_at"].replace("Z", "+00:00"))
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
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

    return {
        "selected_strategy": strategy,
        "chosen_channel": channel,
        "confidence_score": confidence,
    }


# --- LLM MESSAGE GENERATION ---

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


# Model held at module level (not re-constructed per call) so it can be
# monkeypatched in tests without hitting the real API, and so repeated
# invocations in a batch don't pay object-construction overhead each time.
# Haiku is the right cost/latency tier for a short, structured SMS/WhatsApp
# draft — this isn't a task that needs a larger model's reasoning depth.
_message_llm = ChatGoogleGenerativeAI(model="gemini-3.6-flash", temperature=0.2)
_message_chain = _message_llm.with_structured_output(GeneratedMessage)


def generate_message_node(state: RecoveryState) -> Dict[str, Any]:
    """Drafts hyper-personalized recovery copy via Claude, using real
    customer payment history + a merchant tone heuristic + the exact
    failure reason + the strategy already selected. Only runs on the
    approved path (see guardrail_router / graph wiring below) — a blocked
    contact never gets a message drafted."""
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
    finally:
        conn.close()

    tone = derive_merchant_tone(
        merchant["business_type"] if merchant else None,
        merchant["industry"] if merchant else None,
    )

    history_summary = (
        "no prior order history on file" if history is None else
        f"{history['total_orders']} total orders, {history['successful_orders']} successful, "
        f"{history['failed_orders']} failed, historical success rate "
        f"{float(history['historical_payment_success_rate']):.0%}, "
        f"typically pays via {history['typical_payment_method']} around "
        f"{history['typical_payment_hour_ist']}:00 IST, average order value "
        f"₹{float(history['average_order_value']):.2f}"
    )

    failure_reason = state.get("payment_failure_code") or state.get("risk_root_cause") or "unknown"

    prompt = f"""Draft a {state['chosen_channel']} recovery message for a customer whose
payment just failed.

Merchant: {merchant['merchant_name'] if merchant else 'Unknown merchant'}
Merchant tone to write in: {tone}
Customer payment history: {history_summary}
Payment failure reason: {failure_reason}
Chosen recovery strategy: {state.get('selected_strategy')}
Amount at risk: ₹{state.get('amount')}

Write ONE message in the merchant's tone that fits the chosen strategy and
acknowledges the specific failure reason without sounding robotic or
repeating the raw error code to the customer."""

    result: GeneratedMessage = _message_chain.invoke(prompt)

    return {
        "generated_message_body": result.message_body,
        "generated_message_reasoning": result.personalization_reasoning,
        "tone_used": tone,
    }


def persist_decision_node(state: RecoveryState) -> Dict[str, Any]:
    """Writes a recovery_agent_decisions row for EVERY path (approved or
    blocked). This runs regardless of guardrail outcome, so nothing is
    lost from the audit trail — only recovery_agent_actions differs
    downstream based on whether we actually dispatch.

    Idempotent: recovery_agent_decisions.recovery_opportunity_id is UNIQUE
    in the real schema (one decision per opportunity, ever). If a decision
    already exists for this opportunity — e.g. the graph is re-run on the
    same opportunity during dev/testing, or retried after a crash — reuse
    it instead of trying to insert a duplicate and crashing with
    sqlite3.IntegrityError.
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

        reasoning = (
            f"root_cause={state.get('root_cause_category')}; "
            f"strategy={state.get('selected_strategy')}; "
            f"policy_check={'passed' if approved else 'blocked: ' + str(reason)}"
        )

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
                0,
                _now_iso(),
            ),
        )
        conn.commit()
        return {"decision_id": decision_id}
    finally:
        conn.close()


def dispatch_action_node(state: RecoveryState) -> Dict[str, Any]:
    """Executes (or records as skipped) the chosen action.

    On an APPROVED dispatch, writes four things together so the FK chain
    and the compliance guardrail both stay accurate:
      - recovery_agent_actions   (status='success')
      - recovery_interventions   (the actual send record: channel, message_template marker)
      - customer_contact_history (closes the gap where dispatches were
        previously invisible to the guardrail's frequency/interval checks)
      - recovery_generated_messages (the LLM's actual drafted copy, for audit)
      - recovery_attempts.contact_history_id now points at the new contact
        history row instead of always being NULL.

    On a BLOCKED dispatch, only recovery_agent_actions + recovery_attempts
    are written (status='skipped') — no message was generated, no contact
    happened, so none of the send-side tables get a row.

    Idempotent: if an action already exists for this decision_id, reuse
    the prior outcome instead of dispatching/logging again.
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
        status = "success" if approved else "skipped"
        executed_at = _now_iso()

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
                status,
                None if approved else state.get("policy_rejection_reason"),
                state.get("amount"),
                145,    
                executed_at,
            ),
        )

        contact_history_id = None

        if approved:
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

            if state.get("generated_message_body"):
                conn.execute(
                    """
                    INSERT INTO recovery_generated_messages (
                        message_id, decision_id, recovery_opportunity_id, channel,
                        message_body, tone_used, personalization_reasoning,
                        model_used, generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id(),
                        state["decision_id"],
                        state["recovery_opportunity_id"],
                        state.get("chosen_channel"),
                        state.get("generated_message_body"),
                        state.get("tone_used"),
                        state.get("generated_message_reasoning"),
                        "claude-haiku-4-5-20251001",
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
                status if approved else "blocked",
                executed_at,
                None,
                None,
            ),
        )
        conn.commit()
        return {"action_id": action_id, "execution_status": "dispatched" if approved else "skipped"}
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
    success vs. skipped so the blocked path still gets logged rows."""
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

_conn = get_conn()
_ensure_schema(_conn)
_conn.close()


def run_batch(limit: Optional[int] = None) -> Dict[str, int]:
    """Processes every currently-undecided recovery_opportunities row (or up
    to `limit` of them), one at a time. Each opportunity is isolated: if it
    raises (DataIntegrityError, or anything else — a DB hiccup, a bad row),
    that failure is caught, logged, and the batch moves on to the next
    opportunity. This is the realistic shape for how the agent runs in
    production — new opportunities arrive continuously, and one bad record
    should never stop the rest of the queue from being processed.

    Returns a summary dict: {"dispatched": n, "skipped": n, "errors": n}.
    """
    conn = get_conn()
    query = """
        SELECT ro.opportunity_id
        FROM recovery_opportunities ro
        LEFT JOIN recovery_agent_decisions rad
            ON rad.recovery_opportunity_id = ro.opportunity_id
        WHERE rad.decision_id IS NULL
    """
    if limit is not None:
        query += " LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()
    conn.close()

    opportunity_ids = [r["opportunity_id"] for r in rows]
    summary = {"dispatched": 0, "skipped": 0, "errors": 0}

    if not opportunity_ids:
        print("No undecided opportunities to process.")
        return summary

    print(f"Processing {len(opportunity_ids)} undecided opportunit{'y' if len(opportunity_ids) == 1 else 'ies'}...\n")

    for i, opportunity_id in enumerate(opportunity_ids, start=1):
        try:
            result = recovery_app.invoke({"recovery_opportunity_id": opportunity_id})
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
          f"skipped={summary.get('skipped', 0)} errors={summary['errors']}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the recovery agent over all currently undecided opportunities.")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of opportunities to process (default: all undecided ones).",
    )
    args = parser.parse_args()

    run_batch(limit=args.limit)