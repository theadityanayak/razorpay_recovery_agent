"""
backend-patch.py — two optional additions to main.py.

The dashboard runs correctly without either of these. Both are additive: no
existing field changes type, no existing field is removed, and no response
shape any current consumer depends on is altered. Applying them makes the
frontend faster and one chart exact instead of sampled.

────────────────────────────────────────────────────────────────────────────
PATCH 1 — expose decision_id on /opportunities   (strongly recommended)
────────────────────────────────────────────────────────────────────────────
The expandable feed row needs the decision behind an opportunity, and both
/decisions/{id} and /messages/{id} are keyed by decision_id. list_opportunities
already SELECTs rad.decision_id and branches on it to compute `status` — it
just isn't a field on OpportunitySummary, so Pydantic drops it on the way out.
There is also no /decisions?opportunity_id= filter, so a row can't look itself
up either.

Without this, the frontend pages the whole /decisions table (~51 requests at
the 200-row cap) to build a reverse map in the browser. With it, expanding a
row is one direct GET.

In class OpportunitySummary, add the field:

    class OpportunitySummary(BaseModel):
        opportunity_id: str
        customer_id: str
        merchant_id: str
        merchant_name: Optional[str] = None
        amount_at_risk: Optional[float] = None
        root_cause: Optional[str] = None
        status: str
        created_at: Optional[str] = None
        decision_id: Optional[str] = None      # <-- add this line

Then in list_opportunities, pass it through in the constructor:

            results.append(OpportunitySummary(
                opportunity_id=r["opportunity_id"],
                customer_id=r["customer_id"],
                merchant_id=r["merchant_id"],
                merchant_name=r["merchant_name"],
                amount_at_risk=r["amount_at_risk"],
                root_cause=r["root_cause"],
                status=row_status,
                created_at=r["created_at"],
                decision_id=r["decision_id"],   # <-- add this line
            ))

Optional but worth it: list_opportunities LEFT JOINs recovery_agent_decisions
without deduplicating. If an opportunity ever carries more than one decision
row, it appears more than once in the feed and LIMIT/OFFSET paging drifts.
Nothing in the current dataset triggers it, but a GROUP BY ro.opportunity_id
on the base query closes it off.

────────────────────────────────────────────────────────────────────────────
PATCH 2 — GET /stats/root-causes                 (nice to have)
────────────────────────────────────────────────────────────────────────────
/stats/summary carries no root-cause breakdown, so the bar chart currently
aggregates the 1,000 most recent opportunities in the browser and labels
itself as a sample. This endpoint aggregates the full table in SQL. The
frontend calls it first and falls back to sampling on 404, so adding it needs
no frontend change at all.

Paste the following into main.py, next to stats_summary():
"""

# --- add near the other response models -------------------------------------

class RootCauseBucket(BaseModel):
    root_cause: Optional[str] = None
    count: int
    amount_at_risk: float


# --- add next to stats_summary() --------------------------------------------

@app.get("/stats/root-causes", response_model=List[RootCauseBucket])
def stats_root_causes():
    """Opportunity volume and value at risk, grouped by root cause."""
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
