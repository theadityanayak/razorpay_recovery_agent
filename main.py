"""
main.py — FastAPI backend for the AI Revenue Recovery Platform frontend.

Read-only API over the same SQLite db agent_graph.py writes to. This is the
contract layer between your backend (agent_graph.py + this file) and
whoever builds the frontend — they should only ever need this file's
endpoints, never direct DB access.

Run:
    pip install fastapi uvicorn
    uvicorn main:app --reload --port 8000

Then browse http://localhost:8000/docs for interactive API docs (free,
auto-generated from the Pydantic response models below).
"""

import sqlite3
from typing import Optional, List
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = "AI Revenue Recovery Platform (SQLite).db"

app = FastAPI(title="AI Revenue Recovery Platform API", version="1.0")

# Dev CORS: Vite's default port. Tighten this to your actual deployed
# frontend origin before this goes anywhere public — "*" or a wide-open
# allow list is a dev convenience, not a production setting.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --- RESPONSE MODELS ---
# These are the actual contract your frontend developer builds against —
# treat changes to these as breaking changes once he's started.

class OpportunitySummary(BaseModel):
    opportunity_id: str
    customer_id: str
    merchant_id: str
    merchant_name: Optional[str] = None
    amount_at_risk: Optional[float] = None
    root_cause: Optional[str] = None
    status: str  # "undecided" | "dispatched" | "skipped"
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


# --- ENDPOINTS ---

@app.get("/opportunities", response_model=List[OpportunitySummary])
def list_opportunities(
    status: Optional[str] = Query(None, description="undecided | dispatched | skipped"),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    conn = get_conn()
    try:
        base_query = """
            SELECT ro.opportunity_id, ro.customer_id, ro.merchant_id,
                   m.merchant_name, rre.amount_at_risk, rre.root_cause,
                   ro.created_at, raa.status AS action_status,
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

        if conditions:
            base_query += " WHERE " + " AND ".join(conditions)

        # Dedup guard: an opportunity should have at most one decision, but
        # nothing in the schema enforces that. If one ever gets two, this
        # keeps the feed and its LIMIT/OFFSET paging from drifting.
        base_query += " GROUP BY ro.opportunity_id"
        base_query += " ORDER BY ro.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(base_query, params).fetchall()

        results = []
        for r in rows:
            if r["decision_id"] is None:
                row_status = "undecided"
            elif r["action_status"] == "success":
                row_status = "dispatched"
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
    FULL table — not a client-side sample. Added per backend-patch.py."""
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
            requires_human_review=bool(row["requires_human_review"]),
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
                requires_human_review=bool(r["requires_human_review"]),
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
    return {"status": "ok", "time": datetime.utcnow().isoformat()}