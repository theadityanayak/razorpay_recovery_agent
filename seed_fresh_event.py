"""
seed_fresh_event.py — inserts synthetic, undecided risk events + recovery
opportunities so agent_graph.py has fresh cases to process.

Two seeding modes:
  1. Order-based (default) — a plain payment failure on an existing order.
  2. Mandate-reauth (--mandate-reauth) — a subscription/mandate event that
     exercises the mandate_requires_reauth branch in triage_root_cause_node,
     which takes priority over generic auth-failure routing.

Both modes deliberately pick real, existing IDs from your data (orders, or
mandates+subscriptions) rather than inventing fake ones, so every foreign
key resolves correctly against your actual tables.

Usage:
    python3 seed_fresh_event.py
    python3 seed_fresh_event.py --root-cause insufficient_funds --amount 2500
    python3 seed_fresh_event.py --count 5
    python3 seed_fresh_event.py --mandate-reauth
    python3 seed_fresh_event.py --mandate-reauth --count 3
"""

import argparse
import random
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = "AI Revenue Recovery Platform (SQLite).db"

# Root causes actually present in revenue_risk_events.root_cause for
# order-based (non-subscription) events.
ROOT_CAUSES = [
    "bank_outage",
    "customer_cancelled",
    "insufficient_funds",
    "card_expired",
    "gateway_timeout",
]

# Root causes actually present on subscription-linked risk events — a
# genuinely different vocabulary from the order-based one above. Confirmed
# against the real data (SELECT DISTINCT root_cause WHERE subscription_id
# IS NOT NULL) rather than guessed.
MANDATE_ROOT_CAUSES = [
    "mandate_reauth_required",
    "subscription_payment_failed",
    "mandate_revoked",
    "subscription_failure",
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_risk_event_and_opportunity(
    conn: sqlite3.Connection,
    order_id,
    subscription_id,
    customer_id: str,
    merchant_id: str,
    event_type: str,
    root_cause: str,
    amount: float,
    note_tag: str,
) -> str:
    """Shared insert logic for both order-based and mandate/subscription-based
    seed cases — only the sourcing of the FKs differs between them."""
    risk_event_id = str(uuid.uuid4())
    opportunity_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO revenue_risk_events (
            risk_event_id, order_id, subscription_id, invoice_id,
            customer_id, merchant_id, event_type, root_cause,
            amount_at_risk, recoverability_ground_truth, detected_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            risk_event_id,
            order_id,
            subscription_id,
            None,
            customer_id,
            merchant_id,
            event_type,
            root_cause,
            amount,
            "naturally_recoverable",
            now,
            f"[seed_fresh_event] {note_tag}",
        ),
    )

    # estimated_value/intervention_cost are placeholders here — the real
    # pipeline (recovery_opportunities generation logic) would compute
    # these from the risk model; this helper isn't trying to replicate that.
    conn.execute(
        """
        INSERT INTO recovery_opportunities (
            opportunity_id, revenue_risk_event_id, customer_id, merchant_id,
            eligible_for_contact, ineligible_reason, estimated_recovery_probability,
            recommended_intervention, estimated_value, intervention_cost, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            opportunity_id,
            risk_event_id,
            customer_id,
            merchant_id,
            1,
            None,
            round(random.uniform(0.3, 0.8), 2),
            "pending_agent_review",  # left null on purpose — deciding this is the agent's job, not the seed script's
            round(amount * 0.1, 2),
            round(random.uniform(5, 50), 2),
            now,
        ),
    )
    return opportunity_id


def seed_one(conn: sqlite3.Connection, root_cause: str, amount: float) -> str:
    order = conn.execute(
        "SELECT order_id, customer_id, merchant_id FROM orders ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if order is None:
        raise RuntimeError("orders table is empty — nothing to attach a risk event to.")

    return _insert_risk_event_and_opportunity(
        conn,
        order_id=order["order_id"],
        subscription_id=None,
        customer_id=order["customer_id"],
        merchant_id=order["merchant_id"],
        event_type="payment_failed",
        root_cause=root_cause,
        amount=amount,
        note_tag=f"synthetic order-based test event, root_cause={root_cause}",
    )


def _find_reauth_mandate(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT s.subscription_id, s.order_id, s.customer_id, s.merchant_id, m.mandate_id
        FROM mandates m
        JOIN subscriptions s ON s.subscription_id = m.subscription_id
        WHERE m.requires_reauth = 1 OR m.requires_reauth = '1'
        ORDER BY RANDOM() LIMIT 1
        """
    ).fetchone()


def seed_mandate_reauth(conn: sqlite3.Connection, root_cause: str, amount: float) -> str:
    """Seeds a risk event that exercises the mandate_requires_reauth path in
    triage_root_cause_node. Finds a real mandate already flagged
    requires_reauth=1, follows it to its subscription, and uses the
    subscription's own order_id/customer_id/merchant_id — mirroring how
    seed_one follows orders, just one join deeper through
    mandates -> subscriptions.

    Fallback: if no mandate currently has requires_reauth=1 (this dataset
    only ships with exactly one such row), this picks a random mandate and
    flips it to requires_reauth=1 so the demo always has a real case to
    run against. This MUTATES real data in your DB — it's loud about it
    on purpose, since silently rewriting a mandate's state would be a
    surprising side effect to hide inside a "seed" script.
    """
    row = _find_reauth_mandate(conn)

    if row is None:
        candidate = conn.execute(
            """
            SELECT m.mandate_id, s.subscription_id, s.order_id, s.customer_id, s.merchant_id
            FROM mandates m
            JOIN subscriptions s ON s.subscription_id = m.subscription_id
            ORDER BY RANDOM() LIMIT 1
            """
        ).fetchone()
        if candidate is None:
            raise RuntimeError("No mandates/subscriptions exist at all — can't seed a mandate-reauth case.")

        print(f"  [WARNING] No mandate with requires_reauth=1 found in the DB. "
              f"Flipping mandate_id={candidate['mandate_id']} to requires_reauth=1 "
              f"so this demo case has a real record to run against. "
              f"This is a permanent change to your local database.")
        conn.execute(
            "UPDATE mandates SET requires_reauth = 1 WHERE mandate_id = ?",
            (candidate["mandate_id"],),
        )
        row = candidate

    return _insert_risk_event_and_opportunity(
        conn,
        order_id=row["order_id"],
        subscription_id=row["subscription_id"],
        customer_id=row["customer_id"],
        merchant_id=row["merchant_id"],
        event_type="mandate_failed",
        root_cause=root_cause,
        amount=amount,
        note_tag=f"synthetic mandate-reauth test event, root_cause={root_cause}",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root-cause",
        choices=ROOT_CAUSES + MANDATE_ROOT_CAUSES,
        default=None,
        help="Force a specific root cause (default: random from the relevant taxonomy).",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=None,
        help="Force a specific amount_at_risk (default: random 500-50000).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="How many fresh, undecided opportunities to seed (default: 1).",
    )
    parser.add_argument(
        "--mandate-reauth",
        action="store_true",
        help="Seed a subscription/mandate-linked event that exercises the "
             "mandate_requires_reauth routing path instead of a plain order-based failure.",
    )
    args = parser.parse_args()

    conn = get_conn()
    try:
        created = []
        for _ in range(args.count):
            amount = args.amount if args.amount is not None else round(random.uniform(500, 50000), 2)
            if args.mandate_reauth:
                root_cause = args.root_cause or random.choice(MANDATE_ROOT_CAUSES)
                opportunity_id = seed_mandate_reauth(conn, root_cause, amount)
            else:
                root_cause = args.root_cause or random.choice(ROOT_CAUSES)
                opportunity_id = seed_one(conn, root_cause, amount)
            created.append((opportunity_id, root_cause, amount))
        conn.commit()
    finally:
        conn.close()

    kind = "mandate-reauth" if args.mandate_reauth else "order-based"
    print(f"\nSeeded {len(created)} fresh (undecided) {kind} opportunit{'y' if len(created)==1 else 'ies'}:\n")
    for opportunity_id, root_cause, amount in created:
        print(f"  opportunity_id={opportunity_id}  root_cause={root_cause}  amount={amount}")
    print("\nRun `python3 agent_graph.py` now — it will pick these up automatically.")


if __name__ == "__main__":
    main()