"""
Crash-recovery tests: the webhook/agent/dispatch pipeline must converge after
an interruption, not silently lose accepted events or half-finished sends.

Simulated crash points:
  1. webhook recorded ('received') but never handled  -> reconciler re-drives
  2. dispatch claimed ('processing') but never finalized -> reconciler replays
  3. a fresh 'processing' claim (a live send in progress) -> reconciler leaves it alone
"""

import uuid
from datetime import datetime, timedelta, timezone

import reconcile
from db import get_conn
from ingestion import _record_webhook_event


def _iso(dt):
    return dt.isoformat()


def _old():
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=5))


def test_reconcile_redrives_webhook_event_recorded_before_crash():
    """Crash after the webhook was accepted but before the business rows were
    written: the stored payload alone must rebuild the opportunity and run the
    agent."""
    conn = get_conn()
    payload = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": "pay_CRASHTEST01", "order_id": "order_CRASHTEST01",
            "amount": 750000, "currency": "INR",
            "email": "crash.reconcile@example.invalid",
            "contact": "+919900112244", "error_code": "BAD_REQUEST_ERROR",
        }}},
    }
    pk = _record_webhook_event(
        conn, event_id="evt_CRASHTEST01", event_type="payment.failed",
        payment_id="pay_CRASHTEST01", signature_verified=True,
        detail=None, payload=payload,
    )
    assert pk is not None
    # Age the row so it counts as stranded, then crash-simulate: no handling.
    conn.execute("UPDATE webhook_events SET received_at = ? WHERE webhook_event_id = ?",
                 (_old(), pk))
    conn.commit()

    stats = reconcile.reconcile_pending()
    assert stats["webhook_events_redriven"] >= 1

    row = conn.execute(
        "SELECT status, processed_at FROM webhook_events WHERE webhook_event_id = ?", (pk,)
    ).fetchone()
    assert row["status"] == "processed" and row["processed_at"] is not None

    risk = conn.execute(
        "SELECT * FROM revenue_risk_events WHERE external_payment_id = 'pay_CRASHTEST01'"
    ).fetchone()
    assert risk is not None and risk["root_cause"] == "bad_request_error"
    opp = conn.execute(
        "SELECT * FROM recovery_opportunities WHERE revenue_risk_event_id = ?",
        (risk["risk_event_id"],),
    ).fetchone()
    assert opp is not None
    # The reconciler also ran the agent: a decision must exist.
    assert conn.execute(
        "SELECT 1 FROM recovery_agent_decisions WHERE recovery_opportunity_id = ?",
        (opp["opportunity_id"],),
    ).fetchone() is not None
    conn.close()

    # A second pass must not re-drive the same event (converged, idempotent).
    assert reconcile.reconcile_pending()["webhook_events_redriven"] == 0


def test_reconcile_replays_interrupted_dispatch():
    """Crash after the dispatch claim ('processing' row committed) but before
    the send finished: the reconciler must take the stale claim back and send
    from the stored message body."""
    conn = get_conn()
    old = _old()
    merchant = conn.execute("SELECT merchant_id FROM merchants LIMIT 1").fetchone()["merchant_id"]

    customer_id = f"c-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO customers (customer_id, merchant_id, first_name, last_name, email, phone, "
        "city, state, customer_segment, customer_tenure_days, preferred_payment_method, "
        "communication_preference, optimal_contact_hour_ist, avg_response_delay_mins, "
        "price_sensitivity_score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (customer_id, merchant, "Replay", "Test", "replay@example.invalid", "9112233445",
         None, None, "new", "10", None, None, None, None, None, old),
    )
    risk_id, opp_id, dec_id, act_id, msg_id = (
        f"re{uuid.uuid4().hex[:8]}", f"op{uuid.uuid4().hex[:8]}",
        f"d-{uuid.uuid4().hex[:8]}", f"a-{uuid.uuid4().hex[:8]}",
        f"m-{uuid.uuid4().hex[:8]}",
    )
    conn.execute(
        "INSERT INTO revenue_risk_events (risk_event_id, order_id, subscription_id, invoice_id, "
        "customer_id, merchant_id, event_type, root_cause, amount_at_risk, "
        "recoverability_ground_truth, detected_at, notes, external_payment_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (risk_id, f"order_{uuid.uuid4().hex[:6]}", None, None, customer_id, merchant,
         "payment_failed", "card_declined", "4500", None, old, "test", f"pay_{uuid.uuid4().hex[:8]}"),
    )
    conn.execute(
        "INSERT INTO recovery_opportunities (opportunity_id, revenue_risk_event_id, customer_id, "
        "merchant_id, eligible_for_contact, estimated_value, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (opp_id, risk_id, customer_id, merchant, "1", "4500", old, "open"),
    )
    conn.execute(
        "INSERT INTO recovery_agent_decisions (decision_id, recovery_opportunity_id, "
        "risk_assessment, estimated_recovery_probability, recommended_action, "
        "confidence_score, reasoning_summary, policy_check_result, "
        "requires_human_review, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (dec_id, opp_id, "card_declined", 0.8, "send_payment_link", 0.9,
         "test decision", "passed", 0, old),
    )
    conn.execute(
        "INSERT INTO recovery_generated_messages (message_id, decision_id, "
        "recovery_opportunity_id, channel, message_body, tone_used, "
        "personalization_reasoning, model_used, payment_link_url, generated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (msg_id, dec_id, opp_id, "whatsapp",
         "Hi, your payment of ₹4,500 didn't go through. Pay here: "
         "https://dry-run.example.invalid/l/replaytest01 Reply STOP to opt out.",
         "[template]", "test", "template-fallback",
         "https://dry-run.example.invalid/l/replaytest01", old),
    )
    conn.execute(
        "INSERT INTO recovery_agent_actions (action_id, decision_id, action_type, status, "
        "failure_reason, amount_affected, execution_latency_ms, executed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (act_id, dec_id, "send_payment_link", "processing", None, 4500.0, 0, old),
    )
    conn.commit()

    stats = reconcile.reconcile_pending()
    assert stats["dispatches_replayed"] >= 1

    action = conn.execute(
        "SELECT * FROM recovery_agent_actions WHERE action_id = ?", (act_id,)
    ).fetchone()
    assert action["status"] == "success"          # DRY_RUN send finalizes as success
    dispatch = conn.execute(
        "SELECT * FROM outbound_dispatches WHERE decision_id = ?", (dec_id,)
    ).fetchone()
    assert dispatch is not None and dispatch["dry_run"] == 1
    assert "would_send" in (dispatch["payload_summary"] or "")
    assert conn.execute(
        "SELECT 1 FROM customer_contact_history WHERE customer_id = ?", (customer_id,)
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM recovery_audit_logs WHERE action_id = ? AND event_type = 'dispatch_replayed'",
        (act_id,),
    ).fetchone() is not None
    conn.close()

    # Second pass: converged.
    assert reconcile.reconcile_pending()["dispatches_replayed"] == 0


def test_reconcile_leaves_fresh_processing_claims_alone():
    """A 'processing' action claimed seconds ago is a live send in progress;
    touching it would double-send. The reconciler must skip it."""
    conn = get_conn()
    merchant = conn.execute("SELECT merchant_id FROM merchants LIMIT 1").fetchone()["merchant_id"]
    now_s = _iso(datetime.now(timezone.utc))
    risk_id, opp_id, dec_id = (f"re{uuid.uuid4().hex[:8]}", f"op{uuid.uuid4().hex[:8]}",
                               f"d-{uuid.uuid4().hex[:8]}")
    conn.execute(
        "INSERT INTO revenue_risk_events (risk_event_id, order_id, subscription_id, invoice_id, "
        "customer_id, merchant_id, event_type, root_cause, amount_at_risk, "
        "recoverability_ground_truth, detected_at, notes, external_payment_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (risk_id, f"order_{uuid.uuid4().hex[:6]}", None, None, None, merchant,
         "payment_failed", "card_declined", "100", None, now_s, "test", f"pay_{uuid.uuid4().hex[:8]}"),
    )
    conn.execute(
        "INSERT INTO recovery_opportunities (opportunity_id, revenue_risk_event_id, customer_id, "
        "merchant_id, eligible_for_contact, estimated_value, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (opp_id, risk_id, None, merchant, "1", "100", now_s, "open"),
    )
    conn.execute(
        "INSERT INTO recovery_agent_decisions (decision_id, recovery_opportunity_id, "
        "risk_assessment, estimated_recovery_probability, recommended_action, "
        "confidence_score, reasoning_summary, policy_check_result, "
        "requires_human_review, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (dec_id, opp_id, "card_declined", 0.8, "send_payment_link",
         0.9, "fresh claim test", "passed", 0, now_s),
    )
    dec = {"decision_id": dec_id}
    fresh_id = f"a-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO recovery_agent_actions (action_id, decision_id, action_type, status, "
        "failure_reason, amount_affected, execution_latency_ms, executed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (fresh_id, dec["decision_id"], "send_payment_link", "processing", None, 100.0, 0,
         _iso(datetime.now(timezone.utc))),
    )
    conn.commit()
    stats = reconcile.reconcile_pending()
    assert stats["dispatches_replayed"] == 0
    row = conn.execute(
        "SELECT status FROM recovery_agent_actions WHERE action_id = ?", (fresh_id,)
    ).fetchone()
    assert row["status"] == "processing"
    conn.close()


def test_live_mode_never_resends_uncertain_dispatch(monkeypatch):
    """Crash AFTER the provider may have accepted (live mode): the reconciler
    must NOT place a second outbound call. Twilio has no idempotency key and
    the crashed run recorded no provider message id, so acceptance is
    unknowable — the action is flagged delivery_unknown for manual
    reconciliation instead."""
    import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod.settings, "dry_run", False)

    conn = get_conn()
    old = _old()
    merchant = conn.execute("SELECT merchant_id FROM merchants LIMIT 1").fetchone()["merchant_id"]
    customer_id = f"c-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO customers (customer_id, merchant_id, first_name, last_name, email, phone, "
        "city, state, customer_segment, customer_tenure_days, preferred_payment_method, "
        "communication_preference, optimal_contact_hour_ist, avg_response_delay_mins, "
        "price_sensitivity_score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (customer_id, merchant, "Live", "Crash", "live.crash@example.invalid", "9223344556",
         None, None, "new", "10", None, None, None, None, None, old),
    )
    risk_id, opp_id, dec_id, act_id, msg_id = (
        f"re{uuid.uuid4().hex[:8]}", f"op{uuid.uuid4().hex[:8]}",
        f"d-{uuid.uuid4().hex[:8]}", f"a-{uuid.uuid4().hex[:8]}",
        f"m-{uuid.uuid4().hex[:8]}",
    )
    conn.execute(
        "INSERT INTO revenue_risk_events (risk_event_id, order_id, subscription_id, invoice_id, "
        "customer_id, merchant_id, event_type, root_cause, amount_at_risk, "
        "recoverability_ground_truth, detected_at, notes, external_payment_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (risk_id, f"order_{uuid.uuid4().hex[:6]}", None, None, customer_id, merchant,
         "payment_failed", "card_declined", "3200", None, old, "test", f"pay_{uuid.uuid4().hex[:8]}"),
    )
    conn.execute(
        "INSERT INTO recovery_opportunities (opportunity_id, revenue_risk_event_id, customer_id, "
        "merchant_id, eligible_for_contact, estimated_value, created_at, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (opp_id, risk_id, customer_id, merchant, "1", "3200", old, "open"),
    )
    conn.execute(
        "INSERT INTO recovery_agent_decisions (decision_id, recovery_opportunity_id, "
        "risk_assessment, estimated_recovery_probability, recommended_action, "
        "confidence_score, reasoning_summary, policy_check_result, "
        "requires_human_review, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (dec_id, opp_id, "card_declined", 0.8, "send_payment_link", 0.9,
         "live crash test", "passed", 0, old),
    )
    conn.execute(
        "INSERT INTO recovery_generated_messages (message_id, decision_id, "
        "recovery_opportunity_id, channel, message_body, tone_used, "
        "personalization_reasoning, model_used, payment_link_url, generated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (msg_id, dec_id, opp_id, "whatsapp",
         "Hi, your payment of ₹3,200 didn't go through. Reply STOP to opt out.",
         "[template]", "test", "template-fallback", None, old),
    )
    conn.execute(
        "INSERT INTO recovery_agent_actions (action_id, decision_id, action_type, status, "
        "failure_reason, amount_affected, execution_latency_ms, executed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (act_id, dec_id, "send_payment_link", "processing", None, 3200.0, 0, old),
    )
    conn.commit()

    stats = reconcile_mod.reconcile_pending()
    assert stats["dispatches_replayed"] == 0
    assert stats["dispatches_flagged_unknown"] >= 1

    action = conn.execute(
        "SELECT * FROM recovery_agent_actions WHERE action_id = ?", (act_id,)
    ).fetchone()
    assert action["status"] == "delivery_unknown"
    # The decisive part: no second outbound call was placed.
    assert conn.execute(
        "SELECT 1 FROM outbound_dispatches WHERE decision_id = ?", (dec_id,)
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM customer_contact_history WHERE customer_id = ?", (customer_id,)
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM recovery_audit_logs WHERE action_id = ? "
        "AND event_type = 'dispatch_delivery_unknown'", (act_id,)
    ).fetchone() is not None
    conn.close()

    # Converged: a later pass leaves the flagged row for the human.
    assert reconcile_mod.reconcile_pending()["dispatches_flagged_unknown"] == 0
