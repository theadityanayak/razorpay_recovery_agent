"""
main.py — FastAPI app for the AI Revenue Recovery Platform.

One command runs everything:

    pip install -r requirements.txt
    uvicorn main:app --port 8000

That single process serves:
  - the read API the Vite dashboard talks to (all original GET endpoints and
    response schemas are unchanged),
  - the Razorpay webhook listener (POST /webhooks/razorpay),
  - the human-in-the-loop resolution endpoints
    (POST /decisions/{id}/approve, POST /decisions/{id}/reject),
  - a manual simulator (POST /simulate/event),
  - the autonomous polling worker, hosted via the lifespan context manager
    (scheduler.worker_loop) with graceful shutdown.

Security posture (see config.py for the full story):
  - Webhook signatures are HMAC-SHA256 verified against
    RAZORPAY_WEBHOOK_SECRET. The bypass flag exists for local testing and is
    only honored for loopback clients — a deployed instance can never be fed
    unsigned events from the internet.
  - Mutation endpoints require X-Admin-API-Key when ADMIN_API_KEY is set;
    when it isn't set they answer loopback clients only.
  - DRY_RUN=true (default) means no real messages and no real charges, with
    the exact would-be payloads logged to outbound_dispatches.
  - Live sends retry ONLY on connection-establishment failure (bounded,
    exponential backoff); an uncertain delivery is recorded for manual
    reconciliation and never auto-resent.

Honest limits: BackgroundTasks are in-memory — an event accepted right before
a crash is not retried (the webhook_events table records what arrived, so a
reconcile job is possible). WAL + busy_timeout make the single-writer SQLite
model solid, but they do not make concurrent writes scale; move to Postgres
before running multiple workers.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import agent_graph
import scheduler
from agent_graph import execute_human_approved_dispatch, human_reject_decision
from config import settings
from db import get_conn, init_db

logger = logging.getLogger("recovery_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _truthy(value) -> bool:
    """SQLite columns from CSV-originated exports may store booleans as TEXT
    ('0'/'1'/'true'/'false'); Python truthiness would call '0' True."""
    return str(value).strip().lower() in ("1", "true")

# Webhook payload handling lives in ingestion.py; crash recovery in reconcile.py.
import ingestion
import reconcile
# Inbound customer messages + delivery receipts (Twilio) live in inbound.py.
import inbound

# Razorpay events that open a recovery opportunity.
FAILURE_EVENTS = ingestion.FAILURE_EVENTS
# Razorpay events that close the loop.
RECOVERY_EVENTS = ingestion.RECOVERY_EVENTS


# --- LIFESPAN: DB migration + always-on worker ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.started_at = datetime.now(timezone.utc)
    stop_event = asyncio.Event()
    worker_task = None
    if settings.worker_enabled:
        worker_task = asyncio.create_task(
            scheduler.worker_loop(stop_event, settings.worker_poll_interval_seconds)
        )
        logger.info(
            "Worker started (interval=%ss). API + webhooks + worker are live in this one process.",
            settings.worker_poll_interval_seconds,
        )
    else:
        logger.info("WORKER_ENABLED=false — running API + webhooks only.")
    try:
        yield
    finally:
        # Graceful shutdown: let the in-flight cycle finish, then stop.
        stop_event.set()
        if worker_task is not None:
            try:
                await asyncio.wait_for(worker_task, timeout=30)
            except asyncio.TimeoutError:
                worker_task.cancel()
                logger.warning("Worker did not stop within 30s; cancelled.")


app = FastAPI(title="AI Revenue Recovery Platform API", version=settings.app_version, lifespan=lifespan)

# Dev CORS: Vite's default ports. Tighten CORS_ORIGINS to your actual deployed
# frontend origin before this goes anywhere public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list(),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- AUTH ---

def require_admin(
    request: Request,
    x_admin_api_key: Optional[str] = Header(None),
) -> None:
    """Guards mutation endpoints (/simulate/event, /decisions/*/approve|reject).

    With ADMIN_API_KEY configured: constant-time key check, from anywhere.
    Without it: loopback clients only, so a deployed instance with no key
    configured is still not anonymously writable from the internet.
    """
    if settings.admin_api_key:
        if not x_admin_api_key or not hmac.compare_digest(
            x_admin_api_key, settings.admin_api_key
        ):
            raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-API-Key header.")
        return
    host = request.client.host if request.client else None
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="ADMIN_API_KEY is not configured; mutation endpoints are loopback-only.",
        )


# --- RESPONSE MODELS (original dashboard contract — unchanged) ---
# These are the actual contract the frontend builds against — treat changes
# to these as breaking changes.

class OpportunitySummary(BaseModel):
    opportunity_id: str
    customer_id: str
    merchant_id: str
    merchant_name: Optional[str] = None
    amount_at_risk: Optional[float] = None
    root_cause: Optional[str] = None
    # "undecided" | "dispatched" | "skipped" — plus, since the closed-loop
    # upgrade: "pending_review" (agent held it for a human) and "recovered"
    # (a payment.captured webhook matched it). The dashboard's outcomeFor()
    # degrades unknown values to its neutral "Queued" rendering, so existing
    # frontends keep working against these new values.
    status: str
    created_at: Optional[str] = None
    decision_id: Optional[str] = None


class DecisionDetail(BaseModel):
    decision_id: str
    recovery_opportunity_id: str
    risk_assessment: Optional[str] = None
    recommended_action: Optional[str] = None
    confidence_score: Optional[float] = None
    reasoning_summary: Optional[str] = None
    policy_check_result: Optional[str] = None
    requires_human_review: bool
    decided_at: Optional[str] = None
    # Joined fields
    action_status: Optional[str] = None
    action_failure_reason: Optional[str] = None
    customer_id: Optional[str] = None
    merchant_id: Optional[str] = None
    merchant_name: Optional[str] = None
    amount_at_risk: Optional[float] = None


class GeneratedMessageOut(BaseModel):
    message_id: str
    decision_id: str
    channel: Optional[str] = None
    message_body: Optional[str] = None
    tone_used: Optional[str] = None
    personalization_reasoning: Optional[str] = None
    model_used: Optional[str] = None
    generated_at: Optional[str] = None


class StatsSummary(BaseModel):
    total_decisions: int
    dispatched: int
    skipped: int
    requires_human_review: int
    total_amount_at_risk: float
    total_amount_dispatched: float


class RootCauseBucket(BaseModel):
    root_cause: Optional[str] = None
    count: int
    amount_at_risk: float


# Additive models for the closed-loop endpoints (no existing model changes).

class RecoveryMethodBucket(BaseModel):
    recovery_method: Optional[str] = None
    count: int
    recovered_amount: float


class RecoveryStats(BaseModel):
    recovered_count: int
    total_recovered_amount: float
    attributed_count: int          # recovered AFTER the agent contacted the customer
    attributed_amount: float
    by_method: List[RecoveryMethodBucket]


class WebhookResult(BaseModel):
    status: str                    # processed | duplicate | ignored
    event_type: Optional[str] = None
    opportunity_id: Optional[str] = None
    detail: Optional[str] = None


class SimulateEventRequest(BaseModel):
    amount_inr: float = 1499.0
    currency: str = "INR"
    failure_code: str = "insufficient_funds"
    payment_method: str = "upi"
    customer_name: str = "Demo Customer"
    customer_email: str = "demo.customer@example.com"
    customer_phone: str = "+919999000001"


class RejectRequest(BaseModel):
    reason: Optional[str] = None


# --- ORIGINAL GET ENDPOINTS (unchanged contracts) ---

@app.get("/opportunities", response_model=List[OpportunitySummary])
def list_opportunities(
    status: Optional[str] = Query(
        None, description="undecided | dispatched | skipped | pending_review | recovered | delivery_unknown"
    ),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    conn = get_conn()
    try:
        base_query = """
            SELECT ro.opportunity_id, ro.customer_id, ro.merchant_id,
                   m.merchant_name, rre.amount_at_risk, rre.root_cause,
                   ro.created_at, ro.status AS opportunity_status,
                   raa.status AS action_status,
                   rad.decision_id
            FROM recovery_opportunities ro
            LEFT JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id
            LEFT JOIN merchants m ON m.merchant_id = ro.merchant_id
            LEFT JOIN recovery_agent_decisions rad ON rad.recovery_opportunity_id = ro.opportunity_id
            LEFT JOIN recovery_agent_actions raa ON raa.decision_id = rad.decision_id
        """
        conditions = []
        params: list = []

        if status == "undecided":
            conditions.append("rad.decision_id IS NULL")
        elif status == "dispatched":
            conditions.append("raa.status = 'success'")
        elif status == "skipped":
            conditions.append("raa.status = 'skipped'")
        elif status == "pending_review":
            conditions.append("raa.status = 'pending_review'")
        elif status == "recovered":
            conditions.append("ro.status = 'recovered'")
        elif status == "delivery_unknown":
            conditions.append("raa.status = 'delivery_unknown'")

        if conditions:
            base_query += " WHERE " + " AND ".join(conditions)

        # Dedup guard: an opportunity should have at most one decision, but
        # nothing in the original schema enforced that. The GROUP BY keeps
        # the feed and its LIMIT/OFFSET paging from drifting either way.
        base_query += " GROUP BY ro.opportunity_id"
        base_query += " ORDER BY ro.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(base_query, params).fetchall()

        results = []
        for r in rows:
            if r["opportunity_status"] == "recovered":
                row_status = "recovered"
            elif r["decision_id"] is None:
                row_status = "undecided"
            elif r["action_status"] == "success":
                row_status = "dispatched"
            elif r["action_status"] == "pending_review":
                row_status = "pending_review"
            elif r["action_status"] == "delivery_unknown":
                row_status = "delivery_unknown"
            else:
                row_status = "skipped"
            results.append(OpportunitySummary(
                opportunity_id=r["opportunity_id"],
                customer_id=r["customer_id"],
                merchant_id=r["merchant_id"],
                merchant_name=r["merchant_name"],
                amount_at_risk=r["amount_at_risk"],
                root_cause=r["root_cause"],
                status=row_status,
                created_at=r["created_at"],
                decision_id=r["decision_id"],
            ))
        return results
    finally:
        conn.close()


@app.get("/stats/root-causes", response_model=List[RootCauseBucket])
def stats_root_causes():
    """Opportunity volume and value at risk, grouped by root cause, over the
    FULL table — not a client-side sample."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(rre.root_cause, 'unclassified') AS root_cause,
                   COUNT(*) AS count,
                   COALESCE(SUM(rre.amount_at_risk), 0) AS amount_at_risk
            FROM recovery_opportunities ro
            JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id
            GROUP BY rre.root_cause
            ORDER BY count DESC
            """
        ).fetchall()
        return [
            RootCauseBucket(
                root_cause=r["root_cause"],
                count=r["count"],
                amount_at_risk=float(r["amount_at_risk"]),
            )
            for r in rows
        ]
    finally:
        conn.close()


@app.get("/decisions/{decision_id}", response_model=DecisionDetail)
def get_decision(decision_id: str):
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT rad.*, raa.status AS action_status, raa.failure_reason AS action_failure_reason,
                   ro.customer_id, ro.merchant_id, m.merchant_name, rre.amount_at_risk
            FROM recovery_agent_decisions rad
            LEFT JOIN recovery_agent_actions raa ON raa.decision_id = rad.decision_id
            LEFT JOIN recovery_opportunities ro ON ro.opportunity_id = rad.recovery_opportunity_id
            LEFT JOIN merchants m ON m.merchant_id = ro.merchant_id
            LEFT JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id
            WHERE rad.decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No decision found with id {decision_id}")

        return DecisionDetail(
            decision_id=row["decision_id"],
            recovery_opportunity_id=row["recovery_opportunity_id"],
            risk_assessment=row["risk_assessment"],
            recommended_action=row["recommended_action"],
            confidence_score=row["confidence_score"],
            reasoning_summary=row["reasoning_summary"],
            policy_check_result=row["policy_check_result"],
            requires_human_review=_truthy(row["requires_human_review"]),
            decided_at=row["decided_at"],
            action_status=row["action_status"],
            action_failure_reason=row["action_failure_reason"],
            customer_id=row["customer_id"],
            merchant_id=row["merchant_id"],
            merchant_name=row["merchant_name"],
            amount_at_risk=row["amount_at_risk"],
        )
    finally:
        conn.close()


@app.get("/decisions", response_model=List[DecisionDetail])
def list_decisions(
    requires_human_review: Optional[bool] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    conn = get_conn()
    try:
        query = """
            SELECT rad.*, raa.status AS action_status, raa.failure_reason AS action_failure_reason,
                   ro.customer_id, ro.merchant_id, m.merchant_name, rre.amount_at_risk
            FROM recovery_agent_decisions rad
            LEFT JOIN recovery_agent_actions raa ON raa.decision_id = rad.decision_id
            LEFT JOIN recovery_opportunities ro ON ro.opportunity_id = rad.recovery_opportunity_id
            LEFT JOIN merchants m ON m.merchant_id = ro.merchant_id
            LEFT JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id
        """
        params: list = []
        if requires_human_review is not None:
            query += " WHERE rad.requires_human_review = ?"
            params.append(1 if requires_human_review else 0)
        query += " ORDER BY rad.decided_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        return [
            DecisionDetail(
                decision_id=r["decision_id"],
                recovery_opportunity_id=r["recovery_opportunity_id"],
                risk_assessment=r["risk_assessment"],
                recommended_action=r["recommended_action"],
                confidence_score=r["confidence_score"],
                reasoning_summary=r["reasoning_summary"],
                policy_check_result=r["policy_check_result"],
                requires_human_review=_truthy(r["requires_human_review"]),
                decided_at=r["decided_at"],
                action_status=r["action_status"],
                action_failure_reason=r["action_failure_reason"],
                customer_id=r["customer_id"],
                merchant_id=r["merchant_id"],
                merchant_name=r["merchant_name"],
                amount_at_risk=r["amount_at_risk"],
            )
            for r in rows
        ]
    finally:
        conn.close()


@app.get("/messages/{decision_id}", response_model=GeneratedMessageOut)
def get_message(decision_id: str):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM recovery_generated_messages WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No generated message for decision {decision_id} "
                       f"(this decision may have been blocked, so no message was drafted)",
            )
        return GeneratedMessageOut(**dict(row))
    finally:
        conn.close()


@app.get("/stats/summary", response_model=StatsSummary)
def stats_summary():
    conn = get_conn()
    try:
        total_decisions = conn.execute("SELECT COUNT(*) FROM recovery_agent_decisions").fetchone()[0]
        dispatched = conn.execute("SELECT COUNT(*) FROM recovery_agent_actions WHERE status='success'").fetchone()[0]
        skipped = conn.execute("SELECT COUNT(*) FROM recovery_agent_actions WHERE status='skipped'").fetchone()[0]
        requires_review = conn.execute(
            "SELECT COUNT(*) FROM recovery_agent_decisions WHERE requires_human_review=1"
        ).fetchone()[0]

        total_at_risk = conn.execute(
            """
            SELECT COALESCE(SUM(rre.amount_at_risk), 0)
            FROM recovery_opportunities ro
            JOIN revenue_risk_events rre ON rre.risk_event_id = ro.revenue_risk_event_id
            JOIN recovery_agent_decisions rad ON rad.recovery_opportunity_id = ro.opportunity_id
            """
        ).fetchone()[0]

        total_dispatched_amount = conn.execute(
            """
            SELECT COALESCE(SUM(raa.amount_affected), 0)
            FROM recovery_agent_actions raa
            WHERE raa.status = 'success'
            """
        ).fetchone()[0]

        return StatsSummary(
            total_decisions=total_decisions,
            dispatched=dispatched,
            skipped=skipped,
            requires_human_review=requires_review,
            total_amount_at_risk=float(total_at_risk),
            total_amount_dispatched=float(total_dispatched_amount),
        )
    finally:
        conn.close()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "dry_run": settings.dry_run,
        "version": settings.app_version,
    }


@app.get("/")
def root():
    """Clean root health endpoint: service identity, version, docs pointer."""
    return {
        "status": "ok",
        "service": "AI Revenue Recovery Platform API",
        "version": settings.app_version,
        "docs": "/docs",
        "time": datetime.now(timezone.utc).isoformat(),
    }


# --- CLOSED-LOOP: recovery stats (additive) ---

@app.get("/stats/recovery", response_model=RecoveryStats)
def stats_recovery():
    """Recovered revenue as recorded by the payment.captured webhook handler
    (plus any rows already present in the dataset). "Attributed" rows are
    recoveries that landed after this agent contacted the customer — the
    closed-loop number the platform exists to move."""
    conn = get_conn()
    try:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(CAST(recovered_amount AS REAL)), 0) AS amt
            FROM recovered_revenue
            """
        ).fetchone()
        attributed = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(CAST(rr.recovered_amount AS REAL)), 0) AS amt
            FROM recovered_revenue rr
            JOIN recovery_interventions ri
              ON ri.intervention_id = rr.attributed_to_intervention_id
            """
        ).fetchone()
        by_method = conn.execute(
            """
            SELECT recovery_method, COUNT(*) AS n,
                   COALESCE(SUM(CAST(recovered_amount AS REAL)), 0) AS amt
            FROM recovered_revenue
            GROUP BY recovery_method
            ORDER BY amt DESC
            """
        ).fetchall()
        return RecoveryStats(
            recovered_count=totals["n"],
            total_recovered_amount=float(totals["amt"]),
            attributed_count=attributed["n"],
            attributed_amount=float(attributed["amt"]),
            by_method=[
                RecoveryMethodBucket(
                    recovery_method=r["recovery_method"],
                    count=r["n"],
                    recovered_amount=float(r["amt"]),
                )
                for r in by_method
            ],
        )
    finally:
        conn.close()


# --- WEBHOOK INGESTION ---

def _verify_webhook_signature(body: bytes, signature: Optional[str], client_host: Optional[str]) -> bool:
    """Returns True when the signature was cryptographically verified, False
    when the request was accepted via the loopback-only testing bypass.
    Raises HTTPException when the request must be rejected."""
    loopback = client_host in LOOPBACK_HOSTS
    if settings.razorpay_webhook_secret:
        if settings.razorpay_webhook_bypass and loopback and not signature:
            logger.warning("Webhook accepted without signature (loopback bypass).")
            return False
        if not signature:
            raise HTTPException(status_code=401, detail="Missing X-Razorpay-Signature header.")
        expected = hmac.new(
            settings.razorpay_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
        return True
    # No secret configured: only the explicit local-testing bypass lets an
    # unsigned webhook through, and only from this machine.
    if settings.razorpay_webhook_bypass and loopback:
        logger.warning("Webhook accepted without verification (no secret configured; loopback bypass).")
        return False
    raise HTTPException(
        status_code=503,
        detail="RAZORPAY_WEBHOOK_SECRET is not configured; refusing unverifiable webhooks.",
    )


async def _run_agent_background(opportunity_id: str) -> None:
    """BackgroundTasks entry point: the graph is sync (sqlite3 + LangGraph),
    so it runs in a thread and the event loop keeps serving requests."""
    try:
        result = await asyncio.to_thread(
            agent_graph.run_recovery_for_opportunity, opportunity_id, "webhook"
        )
        logger.info(
            "Agent run for opportunity %s -> %s",
            opportunity_id, result.get("execution_status"),
        )
    except Exception:
        logger.exception("Agent run failed for opportunity %s", opportunity_id)


async def _dispatch_after_approval(decision_id: str) -> None:
    try:
        result = await asyncio.to_thread(execute_human_approved_dispatch, decision_id)
        logger.info("Human-approved dispatch for decision %s -> %s", decision_id, result)
    except Exception:
        logger.exception("Human-approved dispatch failed for decision %s", decision_id)


def _process_webhook_payload(
    payload: dict,
    *,
    event_id: Optional[str],
    signature_verified: bool,
    background_tasks: BackgroundTasks,
) -> WebhookResult:
    """Shared by the real webhook endpoint and /simulate/event so both
    exercise exactly the same ingestion path.

    The webhook_events row is committed in 'received' state BEFORE any
    business row exists; only after handling succeeds is it marked
    'processed'. A crash in between is not a silent loss: the reconciler
    (reconcile.reconcile_pending, run at startup and each worker cycle)
    re-drives stranded 'received' rows from their stored payload.
    """
    event_type, entity, payment_id = ingestion._event_entity(payload)
    if not event_id:
        event_id = f"{event_type}:{payment_id}" if payment_id else None

    conn = get_conn()
    try:
        if event_type not in FAILURE_EVENTS and event_type not in RECOVERY_EVENTS:
            pk = ingestion._record_webhook_event(
                conn,
                event_id=event_id, event_type=event_type or "unknown",
                payment_id=payment_id, signature_verified=signature_verified,
                detail="unsupported event type", payload=payload,
            )
            if pk is None:
                return WebhookResult(status="duplicate", event_type=event_type,
                                     detail="already recorded; no rows created")
            ingestion.mark_webhook_event(conn, pk, "ignored", "unsupported event type")
            return WebhookResult(status="ignored", event_type=event_type,
                                 detail="unsupported event type")

        pk = ingestion._record_webhook_event(
            conn,
            event_id=event_id, event_type=event_type, payment_id=payment_id,
            signature_verified=signature_verified, detail=None, payload=payload,
        )
        if pk is None:
            return WebhookResult(status="duplicate", event_type=event_type,
                                 detail="already processed; no rows created")

        row = conn.execute(
            "SELECT * FROM webhook_events WHERE webhook_event_id = ?", (pk,)
        ).fetchone()
        try:
            result = ingestion.process_webhook_event_record(conn, row)
        except Exception as exc:
            conn.rollback()
            ingestion.mark_webhook_event(conn, pk, "failed", str(exc)[:500])
            raise

        if result.get("opportunity_id"):
            background_tasks.add_task(_run_agent_background, result["opportunity_id"])
            return WebhookResult(
                status="processed", event_type=event_type,
                opportunity_id=result["opportunity_id"],
                detail="opportunity created; agent triggered",
            )
        if result.get("closed") is not None:
            closed = result["closed"]
            return WebhookResult(
                status="processed", event_type=event_type,
                detail=f"matched {closed} open opportunit{'y' if closed == 1 else 'ies'} to recovered revenue",
            )
        return WebhookResult(status=result.get("status", "processed"),
                             event_type=event_type, detail="recorded")
    finally:
        conn.close()


@app.post("/webhooks/razorpay", response_model=WebhookResult)
async def razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_razorpay_signature: Optional[str] = Header(None),
    x_razorpay_event_id: Optional[str] = Header(None),
):
    body = await request.body()
    client_host = request.client.host if request.client else None
    verified = _verify_webhook_signature(body, x_razorpay_signature, client_host)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Body is not valid JSON.")
    return _process_webhook_payload(
        payload,
        event_id=x_razorpay_event_id,
        signature_verified=verified,
        background_tasks=background_tasks,
    )


@app.post("/simulate/event", response_model=WebhookResult)
async def simulate_event(
    req: SimulateEventRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_admin),
):
    """Injects a realistic Razorpay-shaped payment.failed webhook through the
    exact same ingestion path as the real endpoint, so the full autonomous
    pipeline — opportunity creation, agent triage, drafting, dispatch (or
    human review) — can be demoed on demand without touching Razorpay."""
    payment_id = f"pay_sim_{uuid.uuid4().hex[:14]}"
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": f"order_sim_{uuid.uuid4().hex[:14]}",
                    "amount": int(round(req.amount_inr * 100)),
                    "currency": req.currency,
                    "status": "failed",
                    "method": req.payment_method,
                    "email": req.customer_email,
                    "contact": req.customer_phone,
                    "error_code": req.failure_code,
                    "error_description": f"Simulated failure: {req.failure_code}",
                    "notes": {"customer_name": req.customer_name},
                }
            }
        },
    }
    return _process_webhook_payload(
        payload,
        event_id=f"evt_sim_{uuid.uuid4().hex[:14]}",
        signature_verified=False,
        background_tasks=background_tasks,
    )


# --- INBOUND MESSAGES + DELIVERY RECEIPTS (TWILIO) ---

class SimulateReplyRequest(BaseModel):
    customer_phone: str
    body: str
    channel: str = "whatsapp"


async def _twilio_form(request: Request) -> dict:
    """Twilio posts application/x-www-form-urlencoded. Parsed manually so
    python-multipart is not a new dependency."""
    from urllib.parse import parse_qs

    raw = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(raw).items()}


@app.post("/webhooks/twilio/inbound")
async def twilio_inbound_webhook(
    request: Request,
    x_twilio_signature: Optional[str] = Header(None),
):
    """Twilio inbound-message webhook: a customer replied to (or STOPped) a
    recovery message. Signature-verified when TWILIO_AUTH_TOKEN is set,
    loopback-only otherwise. Answers empty TwiML so Twilio gets its 200."""
    form = await _twilio_form(request)
    client_host = request.client.host if request.client else None
    inbound.verify_twilio_signature(
        url=str(request.url), params=form, signature=x_twilio_signature,
        client_host=client_host, loopback_hosts=LOOPBACK_HOSTS,
    )
    from_handle = form.get("From", "")
    channel = "whatsapp" if from_handle.startswith("whatsapp:") else "sms"
    conn = get_conn()
    try:
        result = inbound.handle_inbound_message(
            conn,
            from_handle=from_handle,
            body=form.get("Body", ""),
            provider_message_id=form.get("MessageSid"),
            channel=channel,
        )
    finally:
        conn.close()
    return Response(content="<Response/>", media_type="text/xml",
                    headers={"X-Recovery-Result": result.get("status", "unknown")})


@app.post("/webhooks/twilio/status")
async def twilio_status_webhook(
    request: Request,
    x_twilio_signature: Optional[str] = Header(None),
):
    """Twilio delivery-receipt webhook (queued/sent/delivered/failed/
    undelivered). Matches the dispatch row by MessageSid. A failed or
    undelivered receipt is recorded for human reconciliation and NEVER
    triggers an automatic resend."""
    form = await _twilio_form(request)
    client_host = request.client.host if request.client else None
    inbound.verify_twilio_signature(
        url=str(request.url), params=form, signature=x_twilio_signature,
        client_host=client_host, loopback_hosts=LOOPBACK_HOSTS,
    )
    conn = get_conn()
    try:
        result = inbound.handle_status_callback(
            conn,
            provider_message_id=form.get("MessageSid", ""),
            message_status=(form.get("MessageStatus") or "").lower(),
        )
    finally:
        conn.close()
    return Response(content="<Response/>", media_type="text/xml",
                    headers={"X-Recovery-Result": result.get("status", "unknown")})


@app.post("/simulate/reply")
def simulate_reply(
    req: SimulateReplyRequest,
    _: None = Depends(require_admin),
):
    """Simulates a customer reply (e.g. \"STOP\") through the exact same
    handler the real Twilio inbound webhook uses, so the opt-out and reply
    flows can be demoed end-to-end without a phone."""
    conn = get_conn()
    try:
        return inbound.handle_inbound_message(
            conn,
            from_handle=req.customer_phone,
            body=req.body,
            provider_message_id=f"SM_sim_{uuid.uuid4().hex[:12]}",
            channel=req.channel,
        )
    finally:
        conn.close()


# --- OBSERVABILITY ---

@app.get("/metrics")
def metrics():
    """Operational snapshot: pipeline counts, delivery outcomes, inbound
    activity and agent-run health. Additive endpoint — nothing the
    dashboard already consumes changes shape."""
    started = getattr(app.state, "started_at", None)
    conn = get_conn()
    try:
        def q(sql, params=()):
            return conn.execute(sql, params).fetchone()[0]

        def by(sql, params=()):
            return {r[0] if r[0] is not None else "null": r[1]
                    for r in conn.execute(sql, params).fetchall()}

        return {
            "version": settings.app_version,
            "dry_run": settings.dry_run,
            "uptime_seconds": (
                (datetime.now(timezone.utc) - started).total_seconds() if started else None
            ),
            "opportunities": {
                "total": q("SELECT COUNT(*) FROM recovery_opportunities"),
                "by_status": by(
                    "SELECT COALESCE(status, 'open'), COUNT(*) "
                    "FROM recovery_opportunities GROUP BY COALESCE(status, 'open')"
                ),
            },
            "decisions": {
                "total": q("SELECT COUNT(*) FROM recovery_agent_decisions"),
                "pending_review": q(
                    "SELECT COUNT(*) FROM recovery_agent_decisions "
                    "WHERE requires_human_review IN ('1', 'true', 'True', 1)"
                ),
            },
            "dispatches": {
                "total": q("SELECT COUNT(*) FROM outbound_dispatches"),
                "by_status": by("SELECT status, COUNT(*) FROM outbound_dispatches GROUP BY status"),
                "by_delivery_status": by(
                    "SELECT COALESCE(delivery_status, 'no_receipt'), COUNT(*) "
                    "FROM outbound_dispatches GROUP BY COALESCE(delivery_status, 'no_receipt')"
                ),
            },
            "inbound_messages": {
                "total": q("SELECT COUNT(*) FROM inbound_messages"),
                "opt_outs": q("SELECT COUNT(*) FROM inbound_messages WHERE is_opt_out = 1"),
            },
            "webhook_events": {
                "total": q("SELECT COUNT(*) FROM webhook_events"),
                "by_status": by("SELECT status, COUNT(*) FROM webhook_events GROUP BY status"),
            },
            "agent_runs": {
                "total": q("SELECT COUNT(*) FROM agent_runs"),
                "by_status": by("SELECT status, COUNT(*) FROM agent_runs GROUP BY status"),
                "avg_duration_ms": q(
                    "SELECT CAST(AVG((julianday(finished_at) - julianday(started_at)) "
                    "* 86400000.0) AS INTEGER) FROM agent_runs WHERE finished_at IS NOT NULL"
                ),
            },
            "recovered": {
                "opportunities": q(
                    "SELECT COUNT(*) FROM recovery_opportunities WHERE status = 'recovered'"
                ),
                "revenue_inr": q(
                    "SELECT COALESCE(SUM(CAST(recovered_amount AS REAL)), 0) FROM recovered_revenue"
                ),
            },
        }
    finally:
        conn.close()


# --- HUMAN-IN-THE-LOOP RESOLUTION ---

@app.post("/decisions/{decision_id}/approve")
def approve_decision(
    decision_id: str,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_admin),
):
    """Approves a flagged decision: clears the review flag, logs the human
    decision in recovery_audit_logs, and dispatches the parked message in a
    background task (DRY_RUN-aware like every other send)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT decision_id, requires_human_review, policy_check_result "
            "FROM recovery_agent_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No decision found with id {decision_id}")
        if not _truthy(row["requires_human_review"]):
            raise HTTPException(status_code=409, detail="Decision is not pending human review.")
        if row["policy_check_result"] != "passed":
            raise HTTPException(
                status_code=409,
                detail="Decision was blocked by the policy guardrail; it cannot be approved for sending.",
            )

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE recovery_agent_decisions SET requires_human_review = 0 WHERE decision_id = ?",
            (decision_id,),
        )
        conn.execute(
            """
            INSERT INTO recovery_audit_logs (
                audit_log_id, decision_id, action_id, event_type,
                before_state, after_state, logged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), decision_id, None, "human_approved",
             "pending_review", "approved", now),
        )
        conn.commit()
    finally:
        conn.close()

    background_tasks.add_task(_dispatch_after_approval, decision_id)
    return {
        "decision_id": decision_id,
        "status": "approved",
        "dispatch": "scheduled",
        "dry_run": settings.dry_run,
    }


@app.post("/decisions/{decision_id}/reject")
def reject_decision(
    decision_id: str,
    body: Optional[RejectRequest] = None,
    _: None = Depends(require_admin),
):
    """Rejects a flagged decision: no customer contact goes out, the parked
    action becomes 'skipped' with the reviewer's reason, and the decision
    leaves the review queue. Idempotent-ish: already-resolved decisions
    return 409."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT requires_human_review FROM recovery_agent_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No decision found with id {decision_id}")
        if not _truthy(row["requires_human_review"]):
            raise HTTPException(status_code=409, detail="Decision is not pending human review.")
        conn.execute(
            "UPDATE recovery_agent_decisions SET requires_human_review = 0 WHERE decision_id = ?",
            (decision_id,),
        )
        conn.commit()
    finally:
        conn.close()

    result = human_reject_decision(decision_id, (body.reason if body else None))
    return result
