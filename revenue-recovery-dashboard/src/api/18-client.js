/**
 * Thin fetch wrapper over the FastAPI backend in main.py.
 *
 * Mostly read-only GETs, plus the three mutation endpoints the review queue
 * needs (approve / reject / simulate is not called from the UI). Mutations
 * send X-Admin-API-Key when VITE_ADMIN_API_KEY is set; with no key on either
 * side, the backend only accepts them from loopback anyway.
 */

export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000"
).replace(/\/$/, "");

const ADMIN_API_KEY = import.meta.env?.VITE_ADMIN_API_KEY || null;

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null, cause = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.cause = cause;
  }

  /** 404 from /messages/{id} is an expected outcome, not a failure. */
  get isNotFound() {
    return this.status === 404;
  }

  /** Distinguishes "backend is down / CORS blocked" from "backend said no". */
  get isNetwork() {
    return this.status === 0;
  }
}

async function request(path, { signal, method = "GET", body, admin = false } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (admin && ADMIN_API_KEY) headers["X-Admin-API-Key"] = ADMIN_API_KEY;

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      signal,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    // fetch only rejects on network-level failure. In practice that means the
    // uvicorn server isn't running, or the browser origin isn't in main.py's
    // CORS allow_origins list.
    throw new ApiError(
      `Cannot reach the API at ${API_BASE}. Check that uvicorn is running, and that this page is served from http://localhost:5173 (the only origins main.py allows).`,
      { cause: err }
    );
  }

  if (!res.ok) {
    let detail = null;
    try {
      const body = await res.json();
      detail = typeof body?.detail === "string" ? body.detail : null;
    } catch {
      /* non-JSON error body; fall through to the status text */
    }
    throw new ApiError(detail || `${res.status} ${res.statusText}`, {
      status: res.status,
      detail,
    });
  }

  return res.json();
}

function qs(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

export const api = {
  /** GET /health — liveness. Used by the connection indicator in the top bar. */
  health: (opts) => request("/health", opts),

  /** GET /stats/summary — the four metric cards and the outcome donut. */
  stats: (opts) => request("/stats/summary", opts),

  /**
   * GET /opportunities — the activity feed.
   * `status` is one of undecided | dispatched | skipped, or omitted for all.
   * The backend caps `limit` at 200 (Query(50, le=200)).
   */
  opportunities: ({ status, limit = 50, offset = 0 } = {}, opts) =>
    request(`/opportunities${qs({ status, limit, offset })}`, opts),

  /** GET /decisions/{id} — the full reasoning chain for one decision. */
  decision: (decisionId, opts) =>
    request(`/decisions/${encodeURIComponent(decisionId)}`, opts),

  /** GET /decisions — the review queue when requires_human_review is true. */
  decisions: ({ requiresHumanReview, limit = 50, offset = 0 } = {}, opts) =>
    request(
      `/decisions${qs({
        requires_human_review: requiresHumanReview,
        limit,
        offset,
      })}`,
      opts
    ),

  /**
   * GET /messages/{decision_id} — the drafted WhatsApp/SMS copy.
   * Throws ApiError with status 404 when the decision was blocked and no
   * message was ever drafted. Callers must treat that as a normal outcome.
   */
  message: (decisionId, opts) =>
    request(`/messages/${encodeURIComponent(decisionId)}`, opts),

  /**
   * GET /stats/root-causes — full-table root-cause aggregation for the bar
   * chart.
   */
  rootCauses: (opts) => request("/stats/root-causes", opts),

  /** GET /stats/recovery — closed-loop recovered-revenue totals. */
  recoveryStats: (opts) => request("/stats/recovery", opts),

  /**
   * POST /decisions/{id}/approve — approve a flagged decision and dispatch
   * the parked message (DRY_RUN-aware on the backend).
   */
  approveDecision: (decisionId, opts) =>
    request(`/decisions/${encodeURIComponent(decisionId)}/approve`, {
      method: "POST",
      admin: true,
      ...opts,
    }),

  /**
   * POST /decisions/{id}/reject — dismiss a flagged decision; no customer
   * contact goes out. `reason` is recorded on the action's failure_reason.
   */
  rejectDecision: (decisionId, reason, opts) =>
    request(`/decisions/${encodeURIComponent(decisionId)}/reject`, {
      method: "POST",
      admin: true,
      body: { reason: reason || null },
      ...opts,
    }),
};
