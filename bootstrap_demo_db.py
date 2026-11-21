"""
bootstrap_demo_db.py — build a runnable demo database from nothing.

The app expects the base tables from the original Tonic Fabricate CSV export
(merchants, customers, orders, revenue_risk_events, recovery_opportunities,
...) and refuses to start without them. The CSV export itself is not in this
repository, so a fresh checkout cannot run until a database exists. This
script creates one: the same base schema, a small synthetic dataset (one
merchant, four customers, three open recovery opportunities), and then the
upgrade's own migrations via db.init_db().

Usage:

    python bootstrap_demo_db.py              # builds DB at DB_PATH (or its default)
    python bootstrap_demo_db.py --force      # overwrite an existing demo DB

The schema below mirrors tests/conftest.py, which mirrors the CSV export's
table shapes. The dataset is synthetic demo data, not the original export.
"""

import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

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

NOW = datetime.now(timezone.utc).isoformat()
M1 = "m1-" + uuid.uuid4().hex[:8]


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None,
                        help="Target SQLite path (default: DB_PATH from .env / config default)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args()

    if args.db:
        os.environ["DB_PATH"] = args.db
    from config import settings
    db_path = settings.db_path

    if os.path.exists(db_path) and not args.force:
        print(f"Refusing to overwrite existing database: {db_path}\n"
              "Pass --force, or point DB_PATH at a new file.")
        return 1

    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(DDL)

    cur.execute(
        "INSERT INTO merchants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (M1, "TestKart", "B2C", "ecommerce", "Bhubaneswar", "Odisha", "India",
         NOW, "T+3", "INR", "active", "10", "4", "2", "21:00-09:00", NOW),
    )
    # Quiet hours 21:00-09:00 IST so daytime demo runs pass the guardrail.
    cur.execute(
        "INSERT INTO contact_policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pol" + uuid.uuid4().hex[:8], M1, "5", "15", "6",
         "email,sms,whatsapp", "21:00", "09:00", "6", "0", "1", NOW),
    )
    customers = [
        ("c1-" + uuid.uuid4().hex[:8], "Ravi", "ravi.kumar@example.invalid"),
        ("c2-" + uuid.uuid4().hex[:8], "Sana", "sana.iyer@example.invalid"),
        ("c3-" + uuid.uuid4().hex[:8], "Mehul", "mehul.shah@example.invalid"),
        ("c4-" + uuid.uuid4().hex[:8], "Priya", "priya.nair@example.invalid"),
    ]
    for cid, first, email in customers:
        cur.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, M1, first, "Demo", email, "+91 98765 00000", "Cuttack",
             "Odisha", "returning", "300", "UPI", "WhatsApp", "12:00-14:00",
             "5", "0.4", NOW),
        )
    cur.execute(
        "INSERT INTO customer_payment_history VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("h" + uuid.uuid4().hex[:8], customers[0][0], "12", "10", "2", "18400",
         "1533.33", "0.83", "UPI", "13", NOW),
    )

    # Three open opportunities: one the agent holds for review, two it
    # auto-dispatches (in DRY_RUN) on the first batch run.
    _opportunity(cur, "op" + uuid.uuid4().hex[:8], "re" + uuid.uuid4().hex[:8],
                 "o1-" + uuid.uuid4().hex[:8], customers[0][0], 2500.0,
                 "insufficient_funds", "insufficient_funds")
    _opportunity(cur, "op" + uuid.uuid4().hex[:8], "re" + uuid.uuid4().hex[:8],
                 "o2-" + uuid.uuid4().hex[:8], customers[1][0], 899.0,
                 "expired_card", "expired_card")
    _opportunity(cur, "op" + uuid.uuid4().hex[:8], "re" + uuid.uuid4().hex[:8],
                 "o3-" + uuid.uuid4().hex[:8], customers[2][0], 12999.0,
                 "bank_outage", "bank_outage")
    conn.commit()
    conn.close()

    import db  # applies the closed-loop upgrade's tables/columns/indexes
    db.init_db()

    print(f"Demo database ready: {db_path}")
    print("Start the app with:  uvicorn main:app --port 8000")
    print("DRY_RUN defaults to true, so the worker and webhooks run end-to-end")
    print("without sending or charging anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
