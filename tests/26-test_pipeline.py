"""
End-to-end tests for the closed-loop upgrade. Run from the project root:

    python -m pytest tests/ -v

Covers: WAL pragmas, the autonomous agent batch (dispatch + human-review
hold), the original GET contracts, webhook ingestion (failure, idempotency,
signature verification), payment.captured closed-loop attribution, the
simulator, HITL approve/reject, and admin auth on mutation endpoints.
"""

import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import agent_graph
import main
from conftest import C1, C2, C4, M1, OP1, OP2, OP3, _DB
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


# --- infrastructure ---

def test_wal_and_busy_timeout():
    conn = get_conn()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
    finally:
        conn.close()


# --- agent batch: dispatch + human-review hold ---

def test_agent_batch_dispatch_and_hold():
    summary = agent_graph.run_batch()
    assert summary["errors"] == 0
    assert summary["dispatched"] == 2        # OP2 (expired card), OP3 (bank outage)
    assert summary["pending_review"] == 1    # OP1 (insufficient funds, conf 0.45 < 0.5)

    # OP1: held — decision flagged, action parked, draft exists with a link,
    # and NO customer contact happened.
    d1 = q1("SELECT * FROM recovery_agent_decisions WHERE recovery_opportunity_id=?", (OP1,))
    assert d1["requires_human_review"] in ("1", 1)
    assert d1["policy_check_result"] == "passed"
    a1 = q1("SELECT * FROM recovery_agent_actions WHERE decision_id=?", (d1["decision_id"],))
    assert a1["status"] == "pending_review"
    m1 = q1("SELECT * FROM recovery_generated_messages WHERE decision_id=?", (d1["decision_id"],))
    assert m1 is not None and "example.invalid" in m1["payment_link_url"]
    assert q1("SELECT COUNT(*) c FROM customer_contact_history WHERE customer_id=?", (C1,))["c"] == 0

    # OP2: auto-dispatched in DRY_RUN — full audit chain written.
    d2 = q1("SELECT * FROM recovery_agent_decisions WHERE recovery_opportunity_id=?", (OP2,))
    assert d2["requires_human_review"] in ("0", 0)
    a2 = q1("SELECT * FROM recovery_agent_actions WHERE decision_id=?", (d2["decision_id"],))
    assert a2["status"] == "success"
    m2 = q1("SELECT * FROM recovery_generated_messages WHERE decision_id=?", (d2["decision_id"],))
    assert m2["model_used"] == "template-fallback"  # no GOOGLE_API_KEY in tests
    assert "example.invalid" in m2["message_body"]           # retry link made it into the copy
    od = q1("SELECT * FROM outbound_dispatches WHERE decision_id=?", (d2["decision_id"],))
    assert od is not None and od["dry_run"] in (1, "1") and od["provider"] == "twilio"
    assert q1("SELECT COUNT(*) c FROM customer_contact_history WHERE customer_id=?", (C2,))["c"] == 1

    # Batch is idempotent: a second run processes nothing new.
    assert agent_graph.run_batch()["dispatched"] == 0


# --- original GET contracts ---

def test_get_contracts_unchanged(client):
    assert client.get("/").json()["status"] == "ok"
    assert client.get("/health").json()["status"] == "ok"

    opps = client.get("/opportunities").json()
    assert opps and set(opps[0]) == {
        "opportunity_id", "customer_id", "merchant_id", "merchant_name",
        "amount_at_risk", "root_cause", "status", "created_at", "decision_id",
    }
    statuses = {o["status"] for o in opps}
    assert "dispatched" in statuses and "pending_review" in statuses

    decisions = client.get("/decisions").json()
    assert decisions and "reasoning_summary" in decisions[0]

    flagged = client.get("/decisions", params={"requires_human_review": True}).json()
    assert len(flagged) == 1
    did = flagged[0]["decision_id"]
    detail = client.get(f"/decisions/{did}").json()
    assert detail["action_status"] == "pending_review"
    msg = client.get(f"/messages/{did}").json()
    assert msg["message_body"]

    summary = client.get("/stats/summary").json()
    assert set(summary) == {
        "total_decisions", "dispatched", "skipped", "requires_human_review",
        "total_amount_at_risk", "total_amount_dispatched",
    }
    assert summary["requires_human_review"] == 1

    rc = client.get("/stats/root-causes").json()
    assert {b["root_cause"] for b in rc} >= {"insufficient_funds", "expired_card", "bank_outage"}


# --- human-in-the-loop approve / reject ---

def test_approve_flow(client):
    did = q1("SELECT decision_id FROM recovery_agent_decisions WHERE recovery_opportunity_id=?", (OP1,))["decision_id"]
    res = client.post(f"/decisions/{did}/approve")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "approved" and body["dry_run"] is True

    # TestClient waits for background tasks: the dispatch already happened.
    assert q1("SELECT requires_human_review FROM recovery_agent_decisions WHERE decision_id=?", (did,))["requires_human_review"] in ("0", 0)
    action = q1("SELECT * FROM recovery_agent_actions WHERE decision_id=?", (did,))
    assert action["status"] == "success"
    assert q1("SELECT COUNT(*) c FROM customer_contact_history WHERE customer_id=?", (C1,))["c"] == 1
    assert q1("SELECT COUNT(*) c FROM outbound_dispatches WHERE decision_id=?", (did,))["c"] >= 1
    events = [r["event_type"] for r in qa(
        "SELECT event_type FROM recovery_audit_logs WHERE decision_id=? ORDER BY logged_at", (did,))]
    assert "human_approved" in events and "human_approved_dispatch" in events

    # Second approve is a conflict, not a duplicate send.
    assert client.post(f"/decisions/{did}/approve").status_code == 409


def test_reject_flow(client):
    # Fresh low-confidence opportunity, run through the graph directly.
    opp, re_id, order = ("opx" + uuid.uuid4().hex[:6], "rex" + uuid.uuid4().hex[:6],
                         "ox" + uuid.uuid4().hex[:6])
    from conftest import NOW
    conn = get_conn()
    try:
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (order, M1, C4, None, "750", "INR", "one_time", "failed", NOW, NOW, None, None))
        conn.execute("INSERT INTO payment_attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("pax" + uuid.uuid4().hex[:6], order, "1", "card", "750", "failed",
                      "insufficient_funds", "Insufficient funds", "insufficient_funds", NOW))
        conn.execute("INSERT INTO revenue_risk_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (re_id, order, None, None, C4, M1, "payment_failed",
                      "insufficient_funds", "750", None, NOW, None, None))
        conn.execute(
            "INSERT INTO recovery_opportunities (opportunity_id, revenue_risk_event_id, "
            "customer_id, merchant_id, eligible_for_contact, ineligible_reason, "
            "estimated_recovery_probability, recommended_intervention, estimated_value, "
            "intervention_cost, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (opp, re_id, C4, M1, "1", None, None, None, "750", None, NOW))
        conn.commit()
    finally:
        conn.close()

    agent_graph.run_recovery_for_opportunity(opp)
    did = q1("SELECT decision_id FROM recovery_agent_decisions WHERE recovery_opportunity_id=?", (opp,))["decision_id"]

    res = client.post(f"/decisions/{did}/reject", json={"reason": "customer already refunded"})
    assert res.status_code == 200 and res.json()["status"] == "rejected"
    action = q1("SELECT * FROM recovery_agent_actions WHERE decision_id=?", (did,))
    assert action["status"] == "skipped" and "refunded" in action["failure_reason"]
    assert q1("SELECT requires_human_review FROM recovery_agent_decisions WHERE decision_id=?", (did,))["requires_human_review"] in ("0", 0)
    # No contact ever went out for C4: the rejection prevented it.
    assert q1("SELECT COUNT(*) c FROM customer_contact_history WHERE customer_id=?", (C4,))["c"] == 0
    assert client.post(f"/decisions/{did}/reject").status_code == 409


# --- webhook ingestion ---

def _failed_payment_payload(payment_id, order_id, amount_paise=149900):
    return {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id, "amount": amount_paise,
            "currency": "INR", "status": "failed", "method": "upi",
            "email": "new.customer@example.com", "contact": "+91 90000 12345",
            "error_code": "insufficient_funds",
            "error_description": "Insufficient funds",
            "notes": {"customer_name": "New Customer"},
        }}},
    }


def test_webhook_failure_event_and_idempotency(client):
    payment_id, order_id, event_id = (
        "pay_t" + uuid.uuid4().hex[:10], "order_t" + uuid.uuid4().hex[:10],
        "evt_" + uuid.uuid4().hex[:10])
    before_opps = q1("SELECT COUNT(*) c FROM recovery_opportunities")["c"]
    before_decisions = q1("SELECT COUNT(*) c FROM recovery_agent_decisions")["c"]

    res = client.post("/webhooks/razorpay",
                      content=json.dumps(_failed_payment_payload(payment_id, order_id)),
                      headers={"x-razorpay-event-id": event_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "processed" and body["opportunity_id"]

    # The background agent run already finished: a decision exists.
    opp = body["opportunity_id"]
    assert q1("SELECT COUNT(*) c FROM recovery_agent_decisions WHERE recovery_opportunity_id=?", (opp,))["c"] == 1
    # The webhook-created customer and order resolve through the FK chain.
    re_row = q1("SELECT * FROM revenue_risk_events WHERE external_payment_id=?", (payment_id,))
    assert re_row is not None
    assert q1("SELECT 1 x FROM customers WHERE customer_id=?", (re_row["customer_id"],)) is not None

    # Exact redelivery: no new rows anywhere.
    res2 = client.post("/webhooks/razorpay",
                       content=json.dumps(_failed_payment_payload(payment_id, order_id)),
                       headers={"x-razorpay-event-id": event_id})
    assert res2.json()["status"] == "duplicate"
    # Same payment under a NEW event id: still no duplicate opportunity.
    res3 = client.post("/webhooks/razorpay",
                       content=json.dumps(_failed_payment_payload(payment_id, order_id)),
                       headers={"x-razorpay-event-id": "evt_" + uuid.uuid4().hex[:10]})
    assert res3.json()["status"] == "duplicate"
    assert q1("SELECT COUNT(*) c FROM recovery_opportunities")["c"] == before_opps + 1
    assert q1("SELECT COUNT(*) c FROM recovery_agent_decisions")["c"] == before_decisions + 1


def test_webhook_signature_verification(client, monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "whsec_test123")
    monkeypatch.setattr(settings, "razorpay_webhook_bypass", False)
    payload = json.dumps(_failed_payment_payload(
        "pay_s" + uuid.uuid4().hex[:10], "order_s" + uuid.uuid4().hex[:10])).encode()

    good_sig = hmac.new(b"whsec_test123", payload, hashlib.sha256).hexdigest()
    res = client.post("/webhooks/razorpay", content=payload,
                      headers={"X-Razorpay-Signature": good_sig})
    assert res.status_code == 200 and res.json()["status"] == "processed"
    assert q1("SELECT signature_verified FROM webhook_events WHERE event_type='payment.failed' "
              "ORDER BY received_at DESC LIMIT 1")["signature_verified"] == 1

    bad = client.post("/webhooks/razorpay", content=payload,
                      headers={"X-Razorpay-Signature": "deadbeef",
                               "x-razorpay-event-id": "evt_bad" + uuid.uuid4().hex[:6]})
    assert bad.status_code == 401
    missing = client.post("/webhooks/razorpay", content=payload,
                          headers={"x-razorpay-event-id": "evt_miss" + uuid.uuid4().hex[:6]})
    assert missing.status_code == 401


def test_payment_captured_closes_loop(client):
    # Fail a payment first, then capture the retry on the same order.
    payment_id, order_id = "pay_c" + uuid.uuid4().hex[:10], "order_c" + uuid.uuid4().hex[:10]
    res = client.post("/webhooks/razorpay",
                      content=json.dumps(_failed_payment_payload(payment_id, order_id, 59900)))
    opp = res.json()["opportunity_id"]

    captured = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": "pay_cap" + uuid.uuid4().hex[:10], "order_id": order_id,
            "amount": 59900, "currency": "INR", "status": "captured", "method": "upi",
            "email": "new.customer@example.com", "contact": "+91 90000 12345",
        }}},
    }
    res2 = client.post("/webhooks/razorpay", content=json.dumps(captured))
    assert res2.status_code == 200 and "matched 1" in res2.json()["detail"]

    rr = q1("SELECT * FROM recovered_revenue WHERE recovery_opportunity_id=?", (opp,))
    assert rr is not None and float(rr["recovered_amount"]) == 599.0
    status = q1("SELECT status FROM recovery_opportunities WHERE opportunity_id=?", (opp,))["status"]
    assert status == "recovered"
    feed = client.get("/opportunities", params={"status": "recovered"}).json()
    assert any(o["opportunity_id"] == opp for o in feed)

    stats = client.get("/stats/recovery").json()
    assert stats["recovered_count"] >= 1 and stats["total_recovered_amount"] >= 599.0

    # Redelivery of the same capture does not double-book revenue.
    client.post("/webhooks/razorpay", content=json.dumps(captured))
    assert q1("SELECT COUNT(*) c FROM recovered_revenue WHERE recovery_opportunity_id=?", (opp,))["c"] == 1


def test_unknown_event_ignored(client):
    res = client.post("/webhooks/razorpay",
                      content=json.dumps({"event": "refund.created", "payload": {}}))
    assert res.json()["status"] == "ignored"


# --- simulator + admin auth ---

def test_simulate_event(client):
    res = client.post("/simulate/event", json={"amount_inr": 2499, "failure_code": "expired_card"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "processed"
    # Full pipeline ran in the background task.
    assert q1("SELECT COUNT(*) c FROM recovery_agent_decisions WHERE recovery_opportunity_id=?",
              (body["opportunity_id"],))["c"] == 1


def test_admin_key_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "topsecret")
    assert client.post("/simulate/event", json={}).status_code == 401
    assert client.post("/simulate/event", json={},
                       headers={"X-Admin-API-Key": "wrong"}).status_code == 401
    ok = client.post("/simulate/event", json={}, headers={"X-Admin-API-Key": "topsecret"})
    assert ok.status_code == 200
