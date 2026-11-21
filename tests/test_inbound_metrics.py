"""
Tests for the v2.1 automation pass: inbound replies/STOP, delivery receipts,
broader Razorpay events (payment_link.paid, refund, dispute, subscription
charge), bounded dispatch retries, agent_runs tracing, /metrics, and the
already-recovered contact guard.

Run from the project root: python -m pytest tests/ -v

This file sorts before test_pipeline.py, so every opportunity it creates is
either decided by an explicit agent run here or recovered-and-then-decided —
nothing leaks into test_pipeline's batch counts.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import agent_graph
import dispatchers
import main
from conftest import M1, NOW, _DB
from config import settings
from db import get_conn


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as c:
        yield c


def q1(sql, params=()):
    conn = get_conn()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def qa(sql, params=()):
    conn = get_conn()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def make_customer(phone):
    cid = "cx-" + uuid.uuid4().hex[:8]
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, M1, "Test", "Customer", f"{cid}@example.com", phone, "Cuttack",
             "Odisha", "new", "10", "UPI", "WhatsApp", "12:00-14:00", "5", "0.4", NOW),
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def make_opportunity(customer_id, amount=500.0, subscription_id=None, payment_id=None):
    """Order + risk event + open opportunity, returns opportunity_id."""
    conn = get_conn()
    try:
        order_id = "o-" + uuid.uuid4().hex[:8]
        re_id = "re-" + uuid.uuid4().hex[:8]
        opp_id = "op-" + uuid.uuid4().hex[:8]
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, M1, customer_id, None, str(amount), "INR", "one_time", "failed",
             NOW, NOW, None, None),
        )
        conn.execute(
            "INSERT INTO revenue_risk_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (re_id, order_id, subscription_id, None, customer_id, M1, "payment_failed",
             "expired_card", str(amount), None, NOW, None, payment_id),
        )
        conn.execute(
            """
            INSERT INTO recovery_opportunities (
                opportunity_id, revenue_risk_event_id, customer_id, merchant_id,
                eligible_for_contact, ineligible_reason, estimated_recovery_probability,
                recommended_intervention, estimated_value, intervention_cost, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (opp_id, re_id, customer_id, M1, "1", None, None, None, str(amount), None, NOW),
        )
        conn.commit()
    finally:
        conn.close()
    return opp_id


def post_webhook(client, payload):
    return client.post("/webhooks/razorpay", content=json.dumps(payload))


def resolve_review(opp_id):
    """If the webhook-triggered agent held this opportunity for human review,
    reject it — keeps the shared /decisions?requires_human_review count exact."""
    dec = q1(
        "SELECT decision_id, requires_human_review FROM recovery_agent_decisions "
        "WHERE recovery_opportunity_id=?",
        (opp_id,),
    )
    assert dec is not None, "webhook-triggered agent should have decided this opportunity"
    if str(dec["requires_human_review"]).strip().lower() in ("1", "true"):
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE recovery_agent_decisions SET requires_human_review = 0 "
                "WHERE decision_id = ?",
                (dec["decision_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        agent_graph.human_reject_decision(dec["decision_id"], "test cleanup")


def mark_decided(opp_id):
    """Fabricate a decision row directly so the shared batch test's counts
    stay exact — no agent side effects (review queue, contacts) at all."""
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO recovery_agent_decisions (
                decision_id, recovery_opportunity_id, risk_assessment,
                estimated_recovery_probability, recommended_action,
                confidence_score, reasoning_summary, policy_check_result,
                requires_human_review, decided_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            ("dec-" + uuid.uuid4().hex[:8], opp_id, "test", "0.5",
             "send_payment_link", "0.9", "decided in test", "passed", "0", NOW),
        )
        conn.commit()
    finally:
        conn.close()


# --- inbound replies / STOP / START ---

def test_simulate_reply_stop_then_start(client):
    cid = make_customer("+91 90000 11111")

    r = client.post("/simulate/reply", json={
        "customer_phone": "+91 90000 11111", "body": "STOP", "channel": "whatsapp",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["matched_customer"] is True and body["effect"] == "opt_out"

    # The guardrail now refuses contact for this customer.
    verdict = agent_graph.compliance_guardrail_node({"customer_id": cid, "merchant_id": M1})
    assert verdict["policy_approved"] is False
    assert "opted out" in verdict["policy_rejection_reason"]

    # START reverses it (latest opt-state row wins).
    r = client.post("/simulate/reply", json={
        "customer_phone": "+91 90000 11111", "body": "start", "channel": "whatsapp",
    })
    assert r.json()["effect"] == "opt_in"
    verdict = agent_graph.compliance_guardrail_node({"customer_id": cid, "merchant_id": M1})
    assert verdict.get("policy_approved") is True

    rows = qa("SELECT * FROM inbound_messages WHERE customer_id=? ORDER BY received_at", (cid,))
    assert len(rows) == 2 and rows[0]["is_opt_out"] == 1


def test_twilio_inbound_webhook_idempotent_on_message_sid(client):
    cid = make_customer("+91 90000 22222")
    form = {"From": "+91 90000 22222", "Body": "What is this about?",
            "MessageSid": "SM_dup_test_1"}
    r1 = client.post("/webhooks/twilio/inbound", data=form)
    r2 = client.post("/webhooks/twilio/inbound", data=form)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.headers["x-recovery-result"] == "processed"
    assert r2.headers["x-recovery-result"] == "duplicate"
    assert q1("SELECT COUNT(*) c FROM inbound_messages WHERE provider_message_id=?",
              ("SM_dup_test_1",))["c"] == 1
    # A plain reply is recorded as a reply in contact history.
    assert q1("SELECT COUNT(*) c FROM customer_contact_history "
              "WHERE customer_id=? AND message_status='replied'", (cid,))["c"] == 1


def test_inbound_from_unknown_number_is_recorded_not_matched(client):
    r = client.post("/simulate/reply", json={
        "customer_phone": "+91 95555 55555", "body": "STOP", "channel": "sms",
    })
    assert r.status_code == 200
    assert r.json()["matched_customer"] is False


# --- delivery receipts ---

def test_status_callback_updates_delivery_and_audits(client):
    dispatch_id = "d-" + uuid.uuid4().hex[:8]
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO outbound_dispatches (
                dispatch_id, action_id, decision_id, recovery_opportunity_id,
                channel, provider, provider_message_id, payment_link_url,
                payment_link_id, dry_run, status, payload_summary, created_at
            ) VALUES (?, NULL, ?, ?, 'whatsapp', 'twilio', ?, NULL, NULL, 0, 'sent', '{}', ?)
            """,
            (dispatch_id, "dec-" + uuid.uuid4().hex[:8], "op-" + uuid.uuid4().hex[:8],
             "SM_rcpt_test_1", NOW),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.post("/webhooks/twilio/status",
                    data={"MessageSid": "SM_rcpt_test_1", "MessageStatus": "delivered"})
    assert r.status_code == 200 and r.headers["x-recovery-result"] == "processed"
    row = q1("SELECT delivery_status FROM outbound_dispatches WHERE dispatch_id=?", (dispatch_id,))
    assert row["delivery_status"] == "delivered"
    assert q1("SELECT COUNT(*) c FROM recovery_audit_logs WHERE event_type='delivery_delivered'")["c"] >= 1

    r = client.post("/webhooks/twilio/status",
                    data={"MessageSid": "SM_rcpt_test_1", "MessageStatus": "failed"})
    assert q1("SELECT delivery_status FROM outbound_dispatches WHERE dispatch_id=?",
              (dispatch_id,))["delivery_status"] == "failed"

    # Receipt for an unknown message id is acknowledged, not recorded.
    r = client.post("/webhooks/twilio/status",
                    data={"MessageSid": "SM_unknown_9", "MessageStatus": "delivered"})
    assert r.status_code == 200 and r.headers["x-recovery-result"] == "ignored"


# --- broader Razorpay events ---

def test_payment_link_paid_closes_opportunity_by_reference(client):
    cid = make_customer("+91 90000 33333")
    opp_id = make_opportunity(cid)
    payload = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {
            "id": "plink_test_1",
            "reference_id": opp_id,
            "payment_id": "pay_plink_1",
            "amount": 50000,
            "currency": "INR",
            "status": "paid",
        }}},
    }
    r = post_webhook(client, payload)
    assert r.status_code == 200 and r.json()["status"] == "processed"
    opp = q1("SELECT status, recovered_payment_id FROM recovery_opportunities "
             "WHERE opportunity_id=?", (opp_id,))
    assert opp["status"] == "recovered" and opp["recovered_payment_id"] == "pay_plink_1"
    assert q1("SELECT COUNT(*) c FROM recovered_revenue "
              "WHERE recovery_opportunity_id=? AND successful_payment_id='pay_plink_1'",
              (opp_id,))["c"] == 1

    # The agent must never message a customer who already paid: the run is
    # recorded, skipped, and creates no contact.
    result = agent_graph.run_recovery_for_opportunity(opp_id, "test")
    assert result["execution_status"] == "skipped"
    assert "recovered" in (result.get("policy_rejection_reason") or "")
    assert q1("SELECT COUNT(*) c FROM customer_contact_history WHERE customer_id=?",
              (cid,))["c"] == 0


def test_refund_processed_opens_opportunity_for_original_customer(client):
    cid = make_customer("+91 90000 44444")
    opp_id = make_opportunity(cid, amount=750.0, payment_id="pay_rf_origin")
    payload = {
        "event": "refund.processed",
        "payload": {"refund": {"entity": {
            "id": "rfnd_test_1",
            "payment_id": "pay_rf_origin",
            "amount": 75000,
            "currency": "INR",
            "status": "processed",
        }}},
    }
    r = post_webhook(client, payload)
    assert r.status_code == 200 and r.json()["status"] == "processed"
    new_opp_id = r.json()["opportunity_id"]
    assert new_opp_id != opp_id
    row = q1(
        "SELECT ro.customer_id, rre.event_type, rre.external_payment_id "
        "FROM recovery_opportunities ro "
        "JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id "
        "WHERE ro.opportunity_id=?",
        (new_opp_id,),
    )
    assert row["customer_id"] == cid  # inherited from the original payment's risk event
    assert row["event_type"] == "refund_processed"
    assert row["external_payment_id"] == "rfnd_test_1"
    # Replay is a no-op.
    assert post_webhook(client, payload).json()["status"] == "duplicate"
    # The refund webhook also triggered the agent (BackgroundTasks run
    # synchronously under TestClient), so the child opp already has a
    # decision; resolve it if it was held for review, and decide the origin.
    resolve_review(new_opp_id)
    mark_decided(opp_id)


def test_dispute_created_opens_opportunity(client):
    cid = make_customer("+91 90000 55555")
    origin_opp = make_opportunity(cid, payment_id="pay_dp_origin")
    payload = {
        "event": "dispute.created",
        "payload": {"dispute": {"entity": {
            "id": "dsp_test_1",
            "payment_id": "pay_dp_origin",
            "amount": 50000,
            "currency": "INR",
            "reason_code": "fraud",
        }}},
    }
    r = post_webhook(client, payload)
    assert r.status_code == 200 and r.json()["status"] == "processed"
    resolve_review(r.json()["opportunity_id"])
    mark_decided(origin_opp)


def test_subscription_charged_closes_opportunity(client):
    cid = make_customer("+91 90000 66666")
    opp_id = make_opportunity(cid, subscription_id="sub_test_9")
    payload = {
        "event": "subscription.charged",
        "payload": {"payment": {"entity": {
            "id": "pay_sub_9",
            "subscription_id": "sub_test_9",
            "amount": 50000,
            "currency": "INR",
            "status": "captured",
        }}},
    }
    r = post_webhook(client, payload)
    assert r.status_code == 200 and r.json()["status"] == "processed"
    assert q1("SELECT status FROM recovery_opportunities WHERE opportunity_id=?",
              (opp_id,))["status"] == "recovered"
    # The run on a recovered opportunity is skipped, never a re-contact.
    result = agent_graph.run_recovery_for_opportunity(opp_id, "test")
    assert result["execution_status"] == "skipped"


# --- bounded retry policy ---

def test_retry_only_on_connection_level_failures(monkeypatch):
    monkeypatch.setattr(settings, "dispatch_max_attempts", 3)
    monkeypatch.setattr(settings, "dispatch_retry_backoff_seconds", 0)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("connection refused")  # never reached provider
        return "ok"

    extra = {}
    assert dispatchers._call_with_bounded_retries(flaky, what="test", extra=extra) == "ok"
    assert extra["attempts"] == 3

    def ambiguous():
        raise TimeoutError("timed out waiting for a response")  # may have been sent

    with pytest.raises(TimeoutError):
        dispatchers._call_with_bounded_retries(ambiguous, what="test", extra={})

    def permanent():
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        dispatchers._call_with_bounded_retries(permanent, what="test", extra={})


# --- tracing + metrics ---

def test_agent_run_trace_recorded():
    # An opted-out customer: the guardrail blocks, the run completes as
    # 'skipped', and nothing lands in the human-review queue or the contact
    # budget (opt rows are inbound events, not merchant contacts).
    cid = make_customer("+91 90000 77777")
    opp_id = make_opportunity(cid)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO customer_contact_history VALUES (?,?,?,?,?,?,?)",
            ("ch-" + uuid.uuid4().hex[:8], cid, None, "whatsapp", "opt_out", "1", NOW),
        )
        conn.commit()
    finally:
        conn.close()
    result = agent_graph.run_recovery_for_opportunity(opp_id, "test_trigger")
    run = q1("SELECT * FROM agent_runs WHERE run_id=?", (result["agent_run_id"],))
    assert run is not None
    assert run["status"] == "completed"
    assert run["trigger_source"] == "test_trigger"
    assert run["finished_at"] is not None
    nodes = [n["node"] for n in json.loads(run["node_trace_json"])]
    assert "guardrail" in nodes and "dispatch" in nodes


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    m = r.json()
    assert m["version"] == settings.app_version
    assert m["opportunities"]["total"] > 0
    assert m["inbound_messages"]["total"] >= 3      # STOP, start, reply above
    assert m["inbound_messages"]["opt_outs"] >= 1
    assert m["agent_runs"]["total"] >= 3            # explicit runs in this file
    assert "failed" in m["dispatches"]["by_delivery_status"]  # receipt test above
    assert m["recovered"]["opportunities"] >= 2     # payment_link + subscription
