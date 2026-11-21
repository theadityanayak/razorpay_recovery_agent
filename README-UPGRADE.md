# AI Revenue Recovery Platform — Closed-Loop Upgrade (v2.1)

Turns the original read-only dashboard backend into an autonomous,
closed-loop recovery system: Razorpay webhooks in, agent triage + drafting,
compliance-guardrailed dispatch out, customer replies and delivery receipts
back in, and recovered revenue attributed to the outreach that caused it.

## What's new

1. **Real-time webhook ingestion** — `POST /webhooks/razorpay`
   - HMAC-SHA256 verification of `X-Razorpay-Signature` against
     `RAZORPAY_WEBHOOK_SECRET`. A bypass flag exists for local testing and is
     **only honored for loopback clients** — a deployed instance can never
     accept unsigned webhooks from the internet.
   - Idempotent: `webhook_events` enforces uniqueness on Razorpay's event id
     AND on `(payment_id, event_type)`, so retried deliveries and re-sent
     events for the same payment never create duplicate opportunities,
     decisions or messages.
   - Failure events (`payment.failed`, `subscription.halted`,
     `invoice.expired`) insert the risk event + opportunity and trigger the
     LangGraph agent asynchronously via `BackgroundTasks`.
   - `payment.captured` matches the payment back to open opportunities on the
     same order/subscription, marks them `recovered`, and writes
     `recovered_revenue` — the dashboard now tracks real recovered revenue.
   - `POST /simulate/event` injects a realistic failed payment through the
     exact same ingestion path for demos and testing.

2. **Unified always-on worker** — `uvicorn main:app` starts the API, the
   webhook listener AND the polling worker (asyncio task in the FastAPI
   lifespan, `scheduler.worker_loop`), with graceful shutdown: the in-flight
   cycle finishes before the process exits. `python3 scheduler.py` still
   works standalone.

3. **Durable crash recovery** (`ingestion.py`, `reconcile.py`)
   - Every accepted webhook is committed to `webhook_events` in `received`
     state BEFORE any business row exists, and only marked `processed` after
     handling succeeds — the accepted-then-crashed gap is closed.
   - Dispatches are claimed with a committed `processing` action row BEFORE
     the external call; the row is finalized to `success`/`failed` after.
   - `reconcile.reconcile_pending()` runs at app startup and at the top of
     every worker cycle. It re-drives stranded webhook events from their
     stored payloads, re-runs the agent for decisions that never reached an
     action, and handles interrupted (`processing`) dispatches. Claims are
     atomic (guarded UPDATEs + the UNIQUE index on `decision_id`), and rows
     younger than 60s are left alone so a live send in progress is never
     touched.
   - Interrupted dispatch policy is mode-dependent, because a blind resend
     could duplicate a real customer message. In DRY_RUN the dispatch is
     replayed from the stored message body (nothing external exists to
     duplicate). In LIVE mode the reconciler NEVER auto-resends: Twilio has
     no idempotency key and the crashed run recorded no provider message id,
     so provider acceptance is unknowable — the action is marked
     `delivery_unknown` with an audit row for manual reconciliation against
     the provider dashboard. The same ambiguity covers payment links: a
     crashed run may already have created a real Razorpay link, so no new
     link is minted during reconciliation either.
   - All replay paths are idempotent: re-processing converges instead of
     duplicating risk events, opportunities, recovered revenue or messages.

4. **Pluggable outbound dispatch** (`dispatchers.py`)
   - Razorpay Payment Links for one-click retry, created BEFORE drafting so
     the Gemini prompt (and the template fallback) can include the real URL.
   - WhatsApp / SMS via Twilio, email via Resend.
   - `DRY_RUN=true` (the default): nothing is sent, nothing is charged. The
     exact payload that would go out is logged and stored in
     `outbound_dispatches` with `dry_run=1`, so the pipeline runs end-to-end
     with zero credentials. Dry-run payment links use the deliberately
     non-routable `dry-run.example.invalid` domain — a dry-run URL can never
     be confused with a real Razorpay link. The dashboard reads `dry_run`
     from `/health` and shows a "DRY RUN — simulation mode" banner, so the
     UI never presents simulated sends as real ones (an older backend
     without the field is treated as live).
   - No `GOOGLE_API_KEY` → deterministic template copy, persisted with
     `model_used='template-fallback'` and a `[template]` marker, so nobody
     mistakes it for model output.
   - Default model is `gemini-3.5-flash` (the repo's `gemini-3.6-flash` does
     not exist in the current API). Verified against the official model list
     and deprecation schedule (ai.google.dev/gemini-api/docs/models and
     /docs/deprecations, checked 2026-11-21): `gemini-3.5-flash` is the
     current stable Flash model with no announced shutdown date;
     `gemini-2.5-flash` remains available until 2026-10-16.

5. **Human-in-the-loop**
   - Policy-approved decisions with confidence below
     `HUMAN_REVIEW_CONFIDENCE_THRESHOLD` (default 0.5) are parked as
     `pending_review` — no customer contact — and appear in the dashboard's
     review queue with their drafted message.
   - `POST /decisions/{id}/approve` clears the flag, logs the human decision
     in `recovery_audit_logs`, and dispatches via background task.
   - `POST /decisions/{id}/reject` marks the action `skipped` with the
     reviewer's reason. Both return 409 on already-resolved decisions.
   - `ReviewQueue.jsx` has Approve & Send / Dismiss buttons with optimistic
     updates (rows leave instantly; failures roll back with the error shown).

6. **Inbound replies + STOP/START opt-outs** (`inbound.py`, v2.1)
   - `POST /webhooks/twilio/inbound` receives customer replies. Twilio
     signatures (`X-Twilio-Signature`, HMAC-SHA1 per Twilio's spec) are
     verified whenever `TWILIO_AUTH_TOKEN` is configured; with no token the
     endpoint answers loopback clients only — same posture as the Razorpay
     webhook. Idempotent on `MessageSid`, so Twilio redeliveries are no-ops.
   - STOP (and Twilio's other opt-out keywords) flips the customer's opt
     state; START reverses it. The guardrail reads the LATEST opt-state row
     before every dispatch, so an opt-out takes effect on the very next
     agent run and opted-out customers are never contacted again.
   - Any other reply is recorded (`inbound_messages` + a `replied` contact
     row) for response analytics. Inbound rows never consume the merchant's
     contact-frequency budget — they are customer-initiated, not contacts.
   - `POST /simulate/reply` (admin-gated) drives the exact same handler for
     demos: `curl -X POST localhost:8000/simulate/reply -H 'Content-Type: application/json' -d '{"customer_phone": "+91 98765 00000", "body": "STOP"}'`

7. **Delivery receipts** (v2.1)
   - `POST /webhooks/twilio/status` applies Twilio delivery receipts
     (queued/sent/delivered/failed/undelivered) to the matching
     `outbound_dispatches` row via the provider message id, with terminal
     states audited in `recovery_audit_logs`. Set
     `TWILIO_STATUS_CALLBACK_URL` to your deployed
     `https://<host>/webhooks/twilio/status` and live sends register for
     these callbacks automatically.
   - A failed/undelivered receipt is recorded for human reconciliation and
     NEVER triggers an automatic resend (same gate as `delivery_unknown`).

8. **Broader Razorpay event coverage** (v2.1)
   - New loop-closing events: `payment_link.paid` (matches by the
     `reference_id` stamped on the recovery link — direct attribution, no
     heuristic) and `subscription.charged` (matches by subscription).
   - New risk events: `refund.processed` and `dispute.created` reopen
     revenue as at-risk, inheriting the customer/order from the original
     payment's risk event when we have it. Idempotency keys on the
     refund/dispute id, so one payment can produce several distinct refunds.
   - Race closed: an opportunity already marked `recovered` (webhook landed
     while the agent run was still queued) is skipped by the agent and by
     the worker — a customer who already paid is never messaged.

9. **Bounded send retries, honest about uncertainty** (v2.1)
   - Live sends retry ONLY connection-establishment failures (the request
     provably never reached the provider), with exponential backoff:
     `DISPATCH_MAX_ATTEMPTS` (default 3), `DISPATCH_RETRY_BACKOFF_SECONDS`
     (default 2.0). A timeout or error status after the request may have
     arrived is an uncertain delivery: recorded as failed, never
     auto-retried — duplicate-send risk is a human decision.

10. **Run tracing + metrics** (v2.1)
    - Every agent run writes an `agent_runs` row before the graph starts
      (a crash still leaves evidence) and finalizes it with the outcome,
      chosen strategy, error text and per-node timings.
    - `GET /metrics` reports the operational snapshot: opportunities by
      status, pending review count, dispatch and delivery-receipt outcomes,
      inbound message/STOP counts, webhook processing states, agent-run
      health with average duration, and recovered revenue. Additive — no
      existing response changes shape.

11. **Hardening**
   - Every connection (API, agent, worker, seed script) comes from `db.py`
     with `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000` +
     `foreign_keys=ON`.
   - All configuration/secrets centralized in `config.py`
     (pydantic-settings, `.env`).
   - Mutation endpoints require `X-Admin-API-Key` when `ADMIN_API_KEY` is
     set; with no key configured they answer loopback clients only.
   - `GET /` health endpoint; all original GET endpoints and response
     schemas unchanged.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in what you have — everything is optional
python bootstrap_demo_db.py   # build a runnable demo SQLite database
```

`bootstrap_demo_db.py` creates the base schema (mirroring the original CSV
export's table shapes), a small synthetic dataset — one merchant, four
customers, three open recovery opportunities — and applies this upgrade's
migrations. Without it a fresh checkout has no database and startup refuses
with a clear error naming the missing base tables. To use the real export
instead, point `DB_PATH` at the `AI Revenue Recovery Platform (SQLite).db`
built from it.

## Run

```bash
uvicorn main:app --port 8000
```

Then:
- API docs: http://localhost:8000/docs
- Simulate a failed payment: `curl -X POST localhost:8000/simulate/event -H 'Content-Type: application/json' -d '{"amount_inr": 1499}'`
- Dashboard: `cd revenue-recovery-dashboard && npm install && npm run dev`

For local webhook testing without Razorpay, set `RAZORPAY_WEBHOOK_BYPASS=true`
(loopback only). For a real deployment, set `RAZORPAY_WEBHOOK_SECRET` and
`ADMIN_API_KEY`, keep `DRY_RUN=true` until you're ready, then set
`DRY_RUN=false` with live Razorpay/Twilio/Resend credentials.

## Tests

```bash
python -m pytest tests/ -v          # 26 end-to-end tests, no network
cd revenue-recovery-dashboard
npm run smoke                       # headless UI checks incl. approve/dismiss, dry-run banner, live-computed estimated recovery
npm run build
```

## Honest limits

- **SQLite is single-writer.** WAL + busy_timeout make reads and writes
  coexist smoothly for this workload, but they do not make concurrent writes
  scale. Before running multiple workers or high webhook volume, move to
  Postgres.
- **This build is single-instance only.** SQLite is single-writer; WAL +
  busy_timeout let one API process, its in-process worker and the agent
  coexist, but nothing here survives running two app instances against the
  same file (the atomic claims are per-database, not distributed). One
  process, one machine. Move to Postgres before scaling out.
- **Not certified for live provider traffic.** Every external call path has
  been exercised only in DRY_RUN. Twilio/Resend/Razorpay live credentials,
  sender registration (DLT for India SMS, WhatsApp templates), webhook
  signature replay protection beyond HMAC, and rate limiting are all
  unverified against the real providers. Treat the first live deployment as
  a staged rollout with the provider dashboards open.
- **Crash recovery is reconciliation-based, not transactional across
  process boundaries.** The reconciler re-drives accepted-but-unprocessed
  webhooks and decisions without actions at startup and each worker cycle
  (see section 3). Interrupted dispatches are replayed only in DRY_RUN; in
  live mode they become `delivery_unknown` for manual reconciliation, which
  is the only safe choice without provider idempotency — at the cost that a
  genuinely undelivered message then waits for a human.
- **payment.captured matching** keys on order id / subscription id / original
  failed payment id. A customer who pays through a brand-new checkout with a
  different order id and no link back won't be attributed.
- The review-threshold routing is a heuristic; tune
  `HUMAN_REVIEW_CONFIDENCE_THRESHOLD` to taste.
- Webhook-ingested customers are matched by email/normalized phone or created
  as minimal `webhook_ingested` records.
