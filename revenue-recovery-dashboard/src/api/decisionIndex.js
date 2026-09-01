/**
 * Resolves an opportunity_id to its decision.
 *
 * ── Why this file exists ──────────────────────────────────────────────────
 * The expandable feed row has to show the decision chain and the drafted
 * message for a given opportunity. Both of those are keyed by decision_id:
 *
 *     GET /decisions/{decision_id}
 *     GET /messages/{decision_id}
 *
 * But /opportunities does not return decision_id. The SQL in main.py selects
 * `rad.decision_id`, and list_opportunities even branches on it to compute
 * `status` — it's just not a field on the OpportunitySummary response model,
 * so Pydantic drops it before it reaches the client. There is also no
 * /decisions?opportunity_id= filter, so a row can't look itself up.
 *
 * Two ways to close the gap:
 *
 *   1. Add `decision_id` to OpportunitySummary. One field on the model, one
 *      line in the constructor — see backend-patch.py. This is the right fix:
 *      it's additive and optional, so it breaks no existing consumer, and it
 *      turns row expansion into a single direct lookup.
 *
 *   2. Build the reverse map client-side by paging /decisions and keying on
 *      recovery_opportunity_id. That's what this module does.
 *
 * The app prefers (1) automatically when the field is present and falls back
 * to (2) otherwise, so it runs correctly against main.py exactly as shipped.
 *
 * ── Cost of the fallback ──────────────────────────────────────────────────
 * /decisions caps limit at 200, and the dataset holds ~10,166 decisions, so a
 * complete index is ~51 requests against local SQLite. They're issued a few at
 * a time and the whole DecisionDetail object is cached on the way past — so
 * once the index is warm, expanding a row costs one request for the message
 * and nothing for the decision itself. It is built once per page load, lazily,
 * only when a row is actually expanded.
 */

import { api } from "./client";

const PAGE_SIZE = 200; // main.py: Query(50, le=200)
const CONCURRENCY = 4;
const MAX_PAGES = 200; // hard stop at ~40k decisions

/** opportunity_id -> DecisionDetail */
const byOpportunity = new Map();
/** decision_id -> DecisionDetail */
const byDecision = new Map();

let buildPromise = null;
let indexState = { built: false, count: 0, truncated: false };
const listeners = new Set();

function emit() {
  for (const fn of listeners) fn(indexState);
}

export function subscribeToIndex(fn) {
  listeners.add(fn);
  fn(indexState);
  return () => listeners.delete(fn);
}

export function getIndexState() {
  return indexState;
}

function absorb(decisions) {
  for (const d of decisions) {
    if (!d?.decision_id) continue;
    byDecision.set(d.decision_id, d);
    // A single opportunity can in principle carry more than one decision row.
    // Keep the most recent, matching the DESC ordering the API already uses.
    if (d.recovery_opportunity_id) {
      const existing = byOpportunity.get(d.recovery_opportunity_id);
      if (!existing || (d.decided_at || "") > (existing.decided_at || "")) {
        byOpportunity.set(d.recovery_opportunity_id, d);
      }
    }
  }
}

async function fetchPage(pageIndex) {
  const rows = await api.decisions({
    limit: PAGE_SIZE,
    offset: pageIndex * PAGE_SIZE,
  });
  absorb(rows);
  indexState = { ...indexState, count: byDecision.size };
  emit();
  return rows.length;
}

async function build() {
  // Probe page 0 first: if the dataset is small, we're already done.
  const first = await fetchPage(0);
  if (first < PAGE_SIZE) {
    indexState = { built: true, count: byDecision.size, truncated: false };
    emit();
    return;
  }

  let nextPage = 1;
  let exhausted = false;
  let truncated = false;

  async function worker() {
    while (!exhausted) {
      const page = nextPage++;
      if (page >= MAX_PAGES) {
        truncated = true;
        exhausted = true;
        break;
      }
      const received = await fetchPage(page);
      if (received < PAGE_SIZE) exhausted = true;
    }
  }

  await Promise.all(
    Array.from({ length: CONCURRENCY }, () => worker())
  );

  indexState = { built: true, count: byDecision.size, truncated };
  emit();
}

/** Builds the index once; concurrent callers share the same promise. */
export function ensureIndex() {
  if (indexState.built) return Promise.resolve();
  if (!buildPromise) {
    buildPromise = build().catch((err) => {
      buildPromise = null; // allow a retry on the next expand
      throw err;
    });
  }
  return buildPromise;
}

/**
 * Returns the DecisionDetail for an opportunity, or null when the opportunity
 * is still undecided (the agent hasn't reached it yet — a normal state).
 *
 * @param {string} opportunityId
 * @param {string|null} knownDecisionId  decision_id straight off the
 *        opportunity row, when the backend patch is applied. Skips the index.
 */
export async function resolveDecision(opportunityId, knownDecisionId = null) {
  if (knownDecisionId) {
    const cached = byDecision.get(knownDecisionId);
    if (cached) return cached;
    const decision = await api.decision(knownDecisionId);
    absorb([decision]);
    return decision;
  }

  const cached = byOpportunity.get(opportunityId);
  if (cached) return cached;

  await ensureIndex();
  return byOpportunity.get(opportunityId) || null;
}

/** Test/dev helper — clears the cache so a refresh re-reads the backend. */
export function resetIndex() {
  byOpportunity.clear();
  byDecision.clear();
  buildPromise = null;
  indexState = { built: false, count: 0, truncated: false };
  emit();
}
