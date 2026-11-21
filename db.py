"""
db.py — the one place every database connection in this project comes from.

Both main.py (API + webhooks) and agent_graph.py (the LangGraph agent) call
get_conn() so connection pragmas and schema migrations can never drift apart:

  - journal_mode=WAL: readers (the dashboard) don't block writers (webhooks,
    the worker) and vice versa. WAL reduces lock contention a lot; it does
    NOT eliminate it — SQLite still allows only one writer at a time, which
    is why busy_timeout is set as well.
  - busy_timeout=5000: a writer that hits a lock waits up to 5s for the other
    writer to finish instead of failing instantly with "database is locked".
    Under sustained concurrent write pressure some writes can still time
    out; that is a SQLite property, not something configuration removes.
  - foreign_keys=ON: dangling references fail loudly at insert time.

ensure_schema() is idempotent and runs at app startup (and is safe to call
any time): it creates the tables this project adds on top of the original
Tonic Fabricate export, and adds the columns the closed-loop upgrade needs
to tables that already exist.
"""

import sqlite3

from config import settings

# (sqlite3 imported for IntegrityError handling in ensure_schema)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return _table_exists(conn, table) and any(
        r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})")
    )


# Base tables from the original Tonic Fabricate export that this project
# reads and writes. Missing = you're pointing DB_PATH at the wrong file.
_BASE_TABLES = [
    "merchants", "customers", "orders", "payment_attempts", "mandates",
    "contact_policies", "customer_contact_history", "customer_payment_history",
    "revenue_risk_events", "recovery_opportunities", "recovery_agent_decisions",
    "recovery_agent_actions", "recovery_attempts", "recovery_interventions",
    "recovery_audit_logs", "recovered_revenue",
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent migrations for everything beyond the original CSV schema."""
    missing = [t for t in _BASE_TABLES if not _table_exists(conn, t)]
    if missing:
        raise RuntimeError(
            "Database is missing base tables from the original dataset: "
            + ", ".join(missing)
            + ". Point DB_PATH at the 'AI Revenue Recovery Platform (SQLite).db' "
            "built from the CSV export (see README-UPGRADE.md)."
        )

    # Drafted message bodies. The original schema has no column anywhere for
    # actual message text (recovery_interventions.message_template is a
    # template NAME), so this table carries the real drafted copy.
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
            payment_link_url TEXT,
            generated_at TEXT
        )
        """
    )
    # Databases created before the payment-link column existed get it here.
    if not _column_exists(conn, "recovery_generated_messages", "payment_link_url"):
        conn.execute("ALTER TABLE recovery_generated_messages ADD COLUMN payment_link_url TEXT")

    # Webhook idempotency + audit. event_id is Razorpay's x-razorpay-event-id
    # (or a derived fallback); the UNIQUE index is what makes retried
    # deliveries no-ops. The partial unique index on (payment_id, event_type)
    # stops the same failed payment from ever producing two opportunities,
    # even if Razorpay assigns a fresh event id to a re-sent event.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            webhook_event_id TEXT PRIMARY KEY,
            event_id TEXT,
            event_type TEXT NOT NULL,
            payment_id TEXT,
            signature_verified INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            detail TEXT,
            payload_json TEXT,
            received_at TEXT NOT NULL,
            processed_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_events_event_id "
        "ON webhook_events(event_id) WHERE event_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_events_payment_type "
        "ON webhook_events(payment_id, event_type) WHERE payment_id IS NOT NULL"
    )

    # Every outbound send (or would-be send, in DRY_RUN) lands here: the
    # channel, the provider, the provider's message id, the payment link and
    # the exact payload that was or would have been sent.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_dispatches (
            dispatch_id TEXT PRIMARY KEY,
            action_id TEXT,
            decision_id TEXT NOT NULL,
            recovery_opportunity_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            provider TEXT,
            provider_message_id TEXT,
            payment_link_url TEXT,
            payment_link_id TEXT,
            dry_run INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            payload_summary TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_dispatches_decision "
        "ON outbound_dispatches(decision_id)"
    )
    # Delivery receipts (Twilio status callbacks) land on the dispatch row.
    if not _column_exists(conn, "outbound_dispatches", "delivery_status"):
        conn.execute("ALTER TABLE outbound_dispatches ADD COLUMN delivery_status TEXT")
    if not _column_exists(conn, "outbound_dispatches", "delivery_status_at"):
        conn.execute("ALTER TABLE outbound_dispatches ADD COLUMN delivery_status_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_dispatches_provider_msg "
        "ON outbound_dispatches(provider_message_id)"
    )

    # Inbound customer messages (Twilio inbound webhook + /simulate/reply):
    # the reply/STOP leg of the closed loop. provider_message_id is UNIQUE so
    # Twilio's at-least-once webhook redeliveries are no-ops.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inbound_messages (
            inbound_message_id TEXT PRIMARY KEY,
            customer_id TEXT,
            channel TEXT,
            from_handle TEXT,
            body TEXT,
            provider_message_id TEXT UNIQUE,
            is_opt_out INTEGER NOT NULL DEFAULT 0,
            received_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inbound_messages_customer "
        "ON inbound_messages(customer_id)"
    )

    # One row per agent run: what triggered it, when it started/finished,
    # how it ended, and the per-node trace. This is the observability spine
    # behind /metrics and the audit story for every autonomous decision.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            recovery_opportunity_id TEXT NOT NULL,
            trigger_source TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            selected_strategy TEXT,
            execution_status TEXT,
            error TEXT,
            node_trace_json TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_opportunity "
        "ON agent_runs(recovery_opportunity_id)"
    )

    # Columns added to pre-existing tables. ALTER TABLE ADD COLUMN is
    # additive and safe on a populated database; each is guarded so
    # ensure_schema can run repeatedly.

    # Closed-loop status on opportunities: NULL/'open' -> active,
    # 'recovered' -> a payment.captured webhook matched it.
    if not _column_exists(conn, "recovery_opportunities", "status"):
        conn.execute("ALTER TABLE recovery_opportunities ADD COLUMN status TEXT")
    if not _column_exists(conn, "recovery_opportunities", "recovered_at"):
        conn.execute("ALTER TABLE recovery_opportunities ADD COLUMN recovered_at TEXT")
    if not _column_exists(conn, "recovery_opportunities", "recovered_payment_id"):
        conn.execute("ALTER TABLE recovery_opportunities ADD COLUMN recovered_payment_id TEXT")

    # The Razorpay payment id behind a risk event, so payment.captured can
    # match even when the retry happened on a different order id.
    if not _column_exists(conn, "revenue_risk_events", "external_payment_id"):
        conn.execute("ALTER TABLE revenue_risk_events ADD COLUMN external_payment_id TEXT")

    # One decision per opportunity and one action per decision. These are the
    # database-level backstops for webhook redeliveries and webhook-vs-worker
    # races; the agent also checks before inserting, but the index is what
    # closes the check-then-insert race. Guarded: if pre-existing data
    # violates uniqueness the index is skipped (and the app still works,
    # just without the hard backstop) instead of crashing startup.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_opportunity "
            "ON recovery_agent_decisions(recovery_opportunity_id)"
        )
    except sqlite3.IntegrityError:
        pass
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_decision "
            "ON recovery_agent_actions(decision_id)"
        )
    except sqlite3.IntegrityError:
        pass

    conn.commit()


def init_db() -> None:
    conn = get_conn()
    try:
        ensure_schema(conn)
    finally:
        conn.close()
