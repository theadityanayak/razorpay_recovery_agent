"""
Test harness: builds a throwaway SQLite database with the same table shapes
as the Tonic Fabricate export (plus a handful of handcrafted rows), then runs
the real app against it. Nothing here touches network, Razorpay, Twilio,
Resend or Gemini — DRY_RUN stays on.
"""

import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

_TMPDIR = tempfile.mkdtemp(prefix="rrp_test_")
_DB = os.path.join(_TMPDIR, "test.db")

os.environ["DB_PATH"] = _DB
os.environ["DRY_RUN"] = "true"
os.environ["WORKER_ENABLED"] = "false"
os.environ["RAZORPAY_WEBHOOK_BYPASS"] = "true"
os.environ.pop("ADMIN_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)
os.environ.pop("RAZORPAY_WEBHOOK_SECRET", None)

NOW = datetime.now(timezone.utc).isoformat()

M1 = "m1-" + uuid.uuid4().hex[:8]
C1 = "c1-" + uuid.uuid4().hex[:8]
C2 = "c2-" + uuid.uuid4().hex[:8]
C3 = "c3-" + uuid.uuid4().hex[:8]
C4 = "c4-" + uuid.uuid4().hex[:8]
O1 = "o1-" + uuid.uuid4().hex[:8]
O2 = "o2-" + uuid.uuid4().hex[:8]
O3 = "o3-" + uuid.uuid4().hex[:8]
RE1, RE2, RE3 = ("re" + uuid.uuid4().hex[:8] for _ in range(3))
OP1, OP2, OP3 = ("op" + uuid.uuid4().hex[:8] for _ in range(3))

DDL = """
CREATE TABLE merchants (
    merchant_id TEXT PRIMARY KEY, merchant_name TEXT, business_type TEXT,
    industry TEXT, city TEXT, state TEXT, country TEXT, onboarding_date TEXT,
    settlement_cycle TEXT, currency TEXT, merchant_status TEXT,
    max_discount_pct TEXT, max_installment_splits TEXT, max_silent_retries TEXT,
    quiet_hours_ist TEXT, created_at TEXT
);
CREATE TABLE contact_policies (
    policy_id TEXT PRIMARY KEY, merchant_id TEXT, max_contacts_per_24h TEXT,
    max_contacts_per_7d TEXT, minimum_contact_interval_hours TEXT,
    allowed_channels TEXT, quiet_hours_start TEXT, quiet_hours_end TEXT,
    max_retry_attempts TEXT, stop_after_success TEXT, stop_after_opt_out TEXT,
    created_at TEXT
);
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY, merchant_id TEXT, first_name TEXT,
    last_name TEXT, email TEXT, phone TEXT, city TEXT, state TEXT,
    customer_segment TEXT, customer_tenure_days TEXT,
    preferred_payment_method TEXT, communication_preference TEXT,
    optimal_contact_hour_ist TEXT, avg_response_delay_mins TEXT,
    price_sensitivity_score TEXT, created_at TEXT
);
CREATE TABLE customer_payment_history (
    history_id TEXT PRIMARY KEY, customer_id TEXT, total_orders TEXT,
    successful_orders TEXT, failed_orders TEXT, total_spend TEXT,
    average_order_value TEXT, historical_payment_success_rate TEXT,
    typical_payment_method TEXT, typical_payment_hour_ist TEXT,
    last_updated_at TEXT
);
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY, merchant_id TEXT, customer_id TEXT,
    product_id TEXT, order_amount TEXT, currency TEXT, order_type TEXT,
    order_status TEXT, created_at TEXT, checkout_started_at TEXT,
    checkout_completed_at TEXT, payment_deadline TEXT
);
CREATE TABLE payment_attempts (
    payment_attempt_id TEXT PRIMARY KEY, order_id TEXT, attempt_number TEXT,
    payment_method TEXT, amount TEXT, attempt_status TEXT, failure_code TEXT,
    failure_reason TEXT, gateway_response_code TEXT, attempted_at TEXT
);
CREATE TABLE mandates (
    mandate_id TEXT PRIMARY KEY, subscription_id TEXT, customer_id TEXT,
    mandate_status TEXT, max_amount TEXT, created_at TEXT, expiry_date TEXT,
    failure_reason TEXT, requires_reauth TEXT
);
CREATE TABLE subscriptions (
    subscription_id TEXT PRIMARY KEY, order_id TEXT, customer_id TEXT,
    product_id TEXT, merchant_id TEXT, billing_cycle TEXT, amount TEXT,
    subscription_status TEXT, retry_count TEXT, mandate_status TEXT,
    started_at TEXT, next_billing_at TEXT
);
CREATE TABLE revenue_risk_events (
    risk_event_id TEXT PRIMARY KEY, order_id TEXT, subscription_id TEXT,
    invoice_id TEXT, customer_id TEXT, merchant_id TEXT, event_type TEXT,
    root_cause TEXT, amount_at_risk TEXT, recoverability_ground_truth TEXT,
    detected_at TEXT, notes TEXT, external_payment_id TEXT
);
CREATE TABLE recovery_opportunities (
    opportunity_id TEXT PRIMARY KEY, revenue_risk_event_id TEXT,
    customer_id TEXT, merchant_id TEXT, eligible_for_contact TEXT,
    ineligible_reason TEXT, estimated_recovery_probability TEXT,
    recommended_intervention TEXT, estimated_value TEXT, intervention_cost TEXT,
    created_at TEXT
);
CREATE TABLE recovery_agent_decisions (
    decision_id TEXT PRIMARY KEY, recovery_opportunity_id TEXT,
    risk_assessment TEXT, estimated_recovery_probability TEXT,
    recommended_action TEXT, confidence_score TEXT, reasoning_summary TEXT,
    policy_check_result TEXT, requires_human_review TEXT, decided_at TEXT
);
CREATE TABLE recovery_agent_actions (
    action_id TEXT PRIMARY KEY, decision_id TEXT, action_type TEXT,
    status TEXT, failure_reason TEXT, amount_affected TEXT,
    execution_latency_ms TEXT, executed_at TEXT
);
CREATE TABLE recovery_attempts (
    recovery_attempt_id TEXT PRIMARY KEY, recovery_opportunity_id TEXT,
    payment_attempt_id TEXT, contact_history_id TEXT, response_id TEXT,
    attempt_type TEXT, status TEXT, dispatched_at TEXT, delivered_at TEXT,
    delivery_latency_ms TEXT
);
CREATE TABLE recovery_interventions (
    intervention_id TEXT PRIMARY KEY, recovery_opportunity_id TEXT,
    channel TEXT, scheduled_at TEXT, executed_at TEXT, status TEXT,
    blocked_reason TEXT, message_template TEXT, created_at TEXT
);
CREATE TABLE customer_contact_history (
    contact_history_id TEXT PRIMARY KEY, customer_id TEXT,
    intervention_id TEXT, channel TEXT, message_status TEXT, opt_out_flag TEXT,
    contacted_at TEXT
);
CREATE TABLE recovery_audit_logs (
    audit_log_id TEXT PRIMARY KEY, decision_id TEXT, action_id TEXT,
    event_type TEXT, before_state TEXT, after_state TEXT, logged_at TEXT
);
CREATE TABLE recovered_revenue (
    recovered_revenue_id TEXT PRIMARY KEY, recovery_opportunity_id TEXT,
    successful_payment_id TEXT, attributed_to_intervention_id TEXT,
    recovered_amount TEXT, original_amount_at_risk TEXT, recovery_method TEXT,
    attribution_confidence TEXT, recovered_at TEXT
);
"""


def _opportunity(cur, opp_id, re_id, order_id, customer_id, amount, root_cause, failure_code):
    cur.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, M1, customer_id, None, str(amount), "INR", "one_time", "failed",
         NOW, NOW, None, None),
    )
    cur.execute(
        "INSERT INTO payment_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pa" + uuid.uuid4().hex[:8], order_id, "1", "upi", str(amount), "failed",
         failure_code, failure_code.replace("_", " "), failure_code, NOW),
    )
    cur.execute(
        "INSERT INTO revenue_risk_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (re_id, order_id, None, None, customer_id, M1, "payment_failed",
         root_cause, str(amount), None, NOW, None, None),
    )
    cur.execute(
        "INSERT INTO recovery_opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (opp_id, re_id, customer_id, M1, "1", None, None, None, str(amount), None, NOW),
    )


def build_db():
    conn = sqlite3.connect(_DB)
    cur = conn.cursor()
    cur.executescript(DDL)
    cur.execute(
        "INSERT INTO merchants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (M1, "TestKart", "B2C", "ecommerce", "Bhubaneswar", "Odisha", "India",
         NOW, "T+3", "INR", "active", "10", "4", "2", "21:00-09:00", NOW),
    )
    # Quiet hours 21:00-09:00 IST — tests run during the day, so the guardrail
    # approves unless a test says otherwise.
    cur.execute(
        "INSERT INTO contact_policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pol" + uuid.uuid4().hex[:8], M1, "5", "15", "6",
         "email,sms,whatsapp", "21:00", "09:00", "6", "0", "1", NOW),
    )
    for cid, first, email in (
        (C1, "Ravi", "ravi.kumar@example.com"),
        (C2, "Sana", "sana.iyer@example.com"),
        (C3, "Mehul", "mehul.shah@example.com"),
        (C4, "Priya", "priya.nair@example.com"),
    ):
        cur.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, M1, first, "Test", email, "+91 98765 00000", "Cuttack",
             "Odisha", "returning", "300", "UPI", "WhatsApp", "12:00-14:00",
             "5", "0.4", NOW),
        )
    cur.execute(
        "INSERT INTO customer_payment_history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("h" + uuid.uuid4().hex[:8], C1, "12", "10", "2", "18400", "1533.33",
         "0.83", "UPI", "13", NOW),
    )
    # OP1: insufficient funds -> low confidence -> held for human review
    _opportunity(cur, OP1, RE1, O1, C1, 2500.0, "insufficient_funds", "insufficient_funds")
    # OP2: expired card -> auto-dispatch via SMS with a retry link
    _opportunity(cur, OP2, RE2, O2, C2, 899.0, "expired_card", "expired_card")
    # OP3: bank outage -> auto-dispatch via WhatsApp
    _opportunity(cur, OP3, RE3, O3, C3, 12999.0, "bank_outage", "bank_outage")
    conn.commit()
    conn.close()


build_db()

import db  # noqa: E402

db.init_db()
