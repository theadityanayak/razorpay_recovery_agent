/**
 * Headless smoke test — no browser required.
 *
 * Bundles App.jsx, renders it into jsdom against a mock of the main.py API,
 * and asserts on the real DOM. It exercises the paths a build can't: fetch
 * handling, the 404 message case, currency formatting, chart aggregation and
 * expanding a feed row.
 *
 *     npm run smoke
 */

import { JSDOM } from "jsdom";
import { build } from "esbuild";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const API = "http://localhost:8000";

/* ── Fixtures: the exact shapes documented in the handoff brief ─────────── */

const STATS = {
  total_decisions: 10166,
  dispatched: 3428,
  skipped: 3659,
  requires_human_review: 2,
  total_amount_at_risk: 244866771.03,
  total_amount_dispatched: 8864751.9,
};

const OPPORTUNITIES = [
  {
    opportunity_id: "51fd2875-96ec-4d71-aa18-d6883a600814",
    customer_id: "5b53ba66-4533-4443-81c9-f9d69331aa75",
    merchant_id: "2f6f2caa-eb5f-405c-83ea-60f56bb43862",
    merchant_name: "VikalpCloud",
    amount_at_risk: 17318.68,
    root_cause: "subscription_failure",
    status: "dispatched",
    created_at: "2026-09-01T05:52:59.301745+00:00",
  },
  {
    opportunity_id: "d1568de2-cb9e-4c19-b609-533938e0831e",
    customer_id: "b0ff4b26-9af4-46dc-a8be-3c2438293bd4",
    merchant_id: "8efd31fe-54dd-4c30-be2e-5b4a73c6e529",
    merchant_name: "ShikshaDirect",
    amount_at_risk: 1875.02,
    root_cause: "upi_timeout",
    // A held row: the guardrail withheld contact and no message exists.
    status: "skipped",
    created_at: "2026-09-01T04:11:02.000000+00:00",
  },
  {
    opportunity_id: "aaaa1111-0000-4000-8000-000000000003",
    customer_id: "cccc3333-0000-4000-8000-000000000003",
    merchant_id: "mmmm4444-0000-4000-8000-000000000003",
    merchant_name: "NirmaanPay",
    amount_at_risk: 92400.5,
    root_cause: "card_decline",
    status: "undecided",
    created_at: "2026-08-31T21:00:00.000000+00:00",
  },
];

const DECISIONS = [
  {
    decision_id: "7751c1a9-ea08-404e-8b31-9772beb11810",
    recovery_opportunity_id: "51fd2875-96ec-4d71-aa18-d6883a600814",
    risk_assessment: "high_recovery_likelihood",
    recommended_action: "request_mandate_reauth",
    confidence_score: 0.81,
    reasoning_summary:
      "Assessed as high_recovery_likelihood (p=0.81); action=request_mandate_reauth; policy_check=passed",
    policy_check_result: "passed",
    requires_human_review: false,
    decided_at: "2026-09-01T05:52:30.000Z",
    action_status: "success",
    action_failure_reason: "",
    customer_id: "5b53ba66-4533-4443-81c9-f9d69331aa75",
    merchant_id: "2f6f2caa-eb5f-405c-83ea-60f56bb43862",
    merchant_name: "VikalpCloud",
    amount_at_risk: 17318.68,
  },
  {
    decision_id: "83d53bb9-f2c1-4c26-bdf1-b0d8108668cd",
    recovery_opportunity_id: "d1568de2-cb9e-4c19-b609-533938e0831e",
    risk_assessment: "low_recovery_likelihood",
    // Divergent on purpose: triage preferred the gateway failure code over
    // the event's stated upi_timeout, so the action isn't the obvious one.
    recommended_action: "offer_alternate_method",
    confidence_score: 0.21,
    reasoning_summary:
      "Assessed as low_recovery_likelihood (p=0.22); action=offer_alternate_method; policy_check=blocked_quiet_hours",
    policy_check_result: "blocked_quiet_hours",
    requires_human_review: false,
    decided_at: "2026-09-01T04:12:00.000Z",
    action_status: "skipped",
    action_failure_reason: "Quiet hours in customer timezone",
    customer_id: "b0ff4b26-9af4-46dc-a8be-3c2438293bd4",
    merchant_id: "8efd31fe-54dd-4c30-be2e-5b4a73c6e529",
    merchant_name: "ShikshaDirect",
    amount_at_risk: 1875.02,
  },
];

const MESSAGES = {
  "7751c1a9-ea08-404e-8b31-9772beb11810": {
    message_id: "14d68236-9efa-4e81-b257-77d25107f894",
    decision_id: "7751c1a9-ea08-404e-8b31-9772beb11810",
    channel: "whatsapp",
    message_body:
      "Hi, your payment didn't go through due to a bank timeout. Tap here to retry safely: [link]",
    tone_used:
      "[heuristic] casual, quick, friendly urgency; neutral, platform-voiced rather than personal",
    personalization_reasoning:
      "Customer typically pays via UPI in the evening; kept tone brief and low-pressure per merchant heuristic.",
    model_used: "claude-haiku-4-5-20251001",
    generated_at: "2026-09-01T05:52:35.313835+00:00",
  },
};

// Two requires_human_review=true cases. These are NOT the same kind of
// problem, and the UI must say so differently for each — this is what was
// silently wrong until it was caught by eye, not by any test:
//   1. A genuine data-integrity error the agent itself raised.
//   2. A legitimate judgement call inherited from the original dataset
//      (the far more common case in practice — 500+ rows in real data
//      against a couple of data-integrity errors).
const FLAGGED_DECISIONS = [
  {
    decision_id: "flag0001-int3-grty-erro-r00000000001",
    recovery_opportunity_id: "flagopp1-0000-0000-0000-000000000001",
    risk_assessment: "data_integrity_error",
    recommended_action: "data_integrity_error",
    confidence_score: null,
    reasoning_summary:
      "Dangling reference on opportunity flagopp1-0000-0000-0000-000000000001: customer_id=ffffffff-0000-0000-0000-000000000000 not found.",
    policy_check_result: "blocked",
    requires_human_review: true,
    decided_at: "2026-09-01T06:00:00.000Z",
    action_status: "skipped",
    action_failure_reason:
      "Dangling reference on opportunity flagopp1-0000-0000-0000-000000000001: customer_id=ffffffff-0000-0000-0000-000000000000 not found.",
    customer_id: "ffffffff-0000-0000-0000-000000000000",
    merchant_id: "2f6f2caa-eb5f-405c-83ea-60f56bb43862",
    merchant_name: "GhostMerchant",
    amount_at_risk: 3000,
  },
  {
    decision_id: "flag0002-judg-emen-tcal-l00000000002",
    recovery_opportunity_id: "flagopp2-0000-0000-0000-000000000002",
    risk_assessment: "moderate_recovery_likelihood",
    recommended_action: "escalate_to_human",
    confidence_score: 0.46,
    reasoning_summary:
      "Assessed as moderate_recovery_likelihood (p=0.46); action=escalate_to_human; policy_check=passed",
    policy_check_result: "passed",
    requires_human_review: true,
    decided_at: "2026-08-15T09:30:00.000Z",
    action_status: null,
    action_failure_reason: "",
    customer_id: "cccc3333-0000-4000-8000-000000000003",
    merchant_id: "8efd31fe-54dd-4c30-be2e-5b4a73c6e529",
    merchant_name: "EduVriksha Learning",
    amount_at_risk: 3810.4,
  },
];

/* ── Mock transport ──────────────────────────────────────────────────────── */

const calls = [];
const mutationCalls = [];
// Toggled mid-run: proves both the empty-queue good-state AND the
// populated queue with real flagged rows, in one test run.
let reviewQueueMode = "empty";

function json(body, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: () => Promise.resolve(body),
  });
}

function mockFetch(url, opts = {}) {
  const path = String(url).replace(API, "");
  const method = opts.method || "GET";
  calls.push(`${method} ${path}`);

  if (method === "POST" && path.endsWith("/approve")) {
    const id = decodeURIComponent(path.split("/decisions/")[1].replace(/\/approve$/, ""));
    mutationCalls.push(`approve:${id}`);
    return json({ decision_id: id, status: "approved", dispatch: "scheduled", dry_run: true });
  }
  if (method === "POST" && path.endsWith("/reject")) {
    const id = decodeURIComponent(path.split("/decisions/")[1].replace(/\/reject$/, ""));
    mutationCalls.push(`reject:${id}`);
    return json({ decision_id: id, status: "rejected" });
  }

  if (path.startsWith("/health")) return json({ status: "ok", time: new Date().toISOString(), dry_run: true });
  if (path.startsWith("/stats/root-causes")) return json({ detail: "Not Found" }, 404);
  if (path.startsWith("/stats/summary")) return json(STATS);

  if (path.startsWith("/opportunities")) {
    const offset = Number(new URLSearchParams(path.split("?")[1] || "").get("offset") || 0);
    return json(offset > 0 ? [] : OPPORTUNITIES);
  }

  if (path.startsWith("/messages/")) {
    const id = decodeURIComponent(path.split("/messages/")[1]);
    const msg = MESSAGES[id];
    return msg
      ? json(msg)
      : json(
          {
            detail: `No generated message for decision ${id} (this decision may have been blocked, so no message was drafted)`,
          },
          404
        );
  }

  if (path.startsWith("/decisions/")) {
    const id = decodeURIComponent(path.split("/decisions/")[1]);
    const found = DECISIONS.find((d) => d.decision_id === id);
    return found ? json(found) : json({ detail: `No decision found with id ${id}` }, 404);
  }

  if (path.startsWith("/decisions")) {
    const params = new URLSearchParams(path.split("?")[1] || "");
    const offset = Number(params.get("offset") || 0);
    if (params.get("requires_human_review") === "true") {
      return json(offset > 0 ? [] : reviewQueueMode === "flagged" ? FLAGGED_DECISIONS : []);
    }
    return json(offset > 0 ? [] : DECISIONS);
  }

  return json({ detail: "unhandled route" }, 404);
}

/* ── Environment ─────────────────────────────────────────────────────────── */

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: "http://localhost:5173/",
  pretendToBeVisual: true,
});

global.window = dom.window;
global.document = dom.window.document;
// Node 22 ships a read-only `navigator` global, so plain assignment throws.
Object.defineProperty(global, "navigator", {
  value: dom.window.navigator,
  configurable: true,
  writable: true,
});
global.HTMLElement = dom.window.HTMLElement;
global.Element = dom.window.Element;
global.Node = dom.window.Node;
global.getComputedStyle = dom.window.getComputedStyle;
global.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 16);
global.cancelAnimationFrame = clearTimeout;
global.fetch = mockFetch;
global.IS_REACT_ACT_ENVIRONMENT = true;

// recharts' ResponsiveContainer needs ResizeObserver, which jsdom lacks.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
global.ResizeObserver = ResizeObserverStub;
dom.window.ResizeObserver = ResizeObserverStub;
dom.window.matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
});
dom.window.scrollTo = () => {};

// recharts warns that its container is 0x0 — jsdom has no layout engine, so
// that is expected here and would only bury the test output.
const realWarn = console.warn;
console.warn = (...args) =>
  /width\(0\) and height\(0\)/.test(String(args[0])) ? undefined : realWarn(...args);

/* ── Bundle and render ───────────────────────────────────────────────────── */

// The bundle must live inside the project so that the externalised bare
// specifiers below resolve against ./node_modules.
const outDir = mkdtempSync(join(process.cwd(), "node_modules", ".rr-smoke-"));
const outFile = join(outDir, "app.cjs");

await build({
  entryPoints: ["src/App.jsx"],
  bundle: true,
  // CJS, not ESM: some transitive deps call require("react") at runtime, and
  // an ESM bundle with react externalised turns that into a throwing shim.
  format: "cjs",
  outfile: outFile,
  jsx: "automatic",
  platform: "node",
  logLevel: "error",
  // React must NOT be bundled: the test script imports react-dom/client
  // separately, and two copies of React in one tree breaks every hook.
  external: ["react", "react-dom", "react/jsx-runtime", "react-dom/client"],
  define: {
      "import.meta.env.VITE_API_BASE_URL": JSON.stringify(API),
      "import.meta.env.VITE_ADMIN_API_KEY": JSON.stringify(""),
    },
});

const appModule = await import(pathToFileURL(outFile).href);
const App = appModule.default?.default ?? appModule.default;
const React = (await import("react")).default;
const { createRoot } = await import("react-dom/client");
const { act } = await import("react");

const root = createRoot(document.getElementById("root"));

const settle = async (ms = 60) => {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
};

await act(async () => {
  root.render(React.createElement(App));
});
await settle(150);

/* ── Assertions ──────────────────────────────────────────────────────────── */

const failures = [];
const text = () => document.body.textContent || "";

function check(name, condition, detail = "") {
  if (condition) console.log(`  ok    ${name}`);
  else {
    console.log(`  FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
    failures.push(name);
  }
}

console.log("\nOverview");
check("stats endpoint called", calls.some((c) => c.endsWith("/stats/summary")));
check("decision count rendered", text().includes("10,166"));
check("dispatched count rendered", text().includes("3,428"));
check("held count rendered", text().includes("3,659"));
check(
  "amount at risk in crore, not millions",
  text().includes("₹24.49 Cr"),
  "expected lakh/crore compaction"
);
// A payment can fail; an agent decision never "fails". The invariant is
// that no outcome is styled red and no outcome pill uses fault language.
const redStyling = document.querySelectorAll(
  '[class*="bg-red"],[class*="text-red"],[class*="border-red"]'
).length;
check("no red styling anywhere in the outcome UI", redStyling === 0, `${redStyling} red-classed nodes`);
const pillText = [...document.querySelectorAll("span")]
  .map((n) => n.textContent || "")
  .filter((t) => /guardrail|Message sent|Awaiting/i.test(t))
  .join(" | ");
check(
  "held outcome worded neutrally",
  pillText.includes("Held by guardrail") && !/fail|error|reject|denied/i.test(pillText)
);
check("root-cause aggregate falls back to sampling", calls.some((c) => c.includes("/stats/root-causes")));
check("root cause labels humanised", text().includes("Subscription failure"));
check("donut contact rate rendered", text().includes("48.4%"));

console.log("\nFeed");
check("merchant rows rendered", text().includes("VikalpCloud") && text().includes("ShikshaDirect"));
check("outcome vocabulary used", text().includes("Message sent") && text().includes("Held by guardrail"));
check("undecided row reads as queued", text().includes("Awaiting triage"));
check("estimated recovery computed from live stats, labelled as an assumption", text().includes("Estimated recovery (assumes 60%): ₹14.69 Cr"));
check("dry-run banner shown when API reports simulation mode", text().includes("DRY RUN — simulation mode"));
check("INR grouping on row amounts", text().includes("₹17,318.68"));

console.log("\nExpanding a dispatched row");
const buttons = () => [...document.querySelectorAll("button[aria-expanded]")];
await act(async () => {
  buttons()[0]?.click();
});
await settle(200);

check("decision index built from /decisions", calls.some((c) => c.includes("/decisions?")));
check("pipeline stages rendered", text().includes("Guardrail") && text().includes("Dispatch"));
check("chosen action humanised", text().includes("Ask for mandate re-authorisation"));
check("reasoning summary shown", text().includes("policy_check=passed"));
check("message body shown", text().includes("Tap here to retry safely"));
check(
  "heuristic tone labelled, bracket not leaked",
  text().includes("Inferred, not merchant-supplied") && !text().includes("[heuristic]")
);
check("model surfaced", text().includes("claude-haiku-4-5-20251001"));

console.log("\nExpanding a held row (message 404)");
await act(async () => {
  buttons()[0]?.click();
});
await settle(80);
await act(async () => {
  buttons()[1]?.click();
});
await settle(200);

check("404 handled as an explanation", text().includes("No message was drafted"));
check("404 does not surface as an error", !/API returned an error/i.test(text()));
check("divergence note shown", text().includes("triage prefers the real gateway failure code"));

console.log("\nReview queue");
const reviewNav = [...document.querySelectorAll("button")].find((b) =>
  (b.textContent || "").includes("Needs review")
);
await act(async () => {
  reviewNav?.click();
});
await settle(150);

check("empty review queue reads as good", text().includes("Nothing needs review"));
check(
  "review filter sent to API",
  calls.some((c) => c.includes("requires_human_review=true"))
);

// Switch the mock to return real flagged rows, then use the app's own
// Refresh button so this exercises the real refresh path, not a re-render
// shortcut.
reviewQueueMode = "flagged";
const refreshBtn = [...document.querySelectorAll("button")].find((b) =>
  (b.textContent || "").includes("Refresh")
);
await act(async () => {
  refreshBtn?.click();
});
await settle(200);

check("flagged rows appear after refresh", text().includes("GhostMerchant") && text().includes("EduVriksha Learning"));

const reviewRowButtons = () =>
  [...document.querySelectorAll("button[aria-expanded]")].filter((b) =>
    (b.getAttribute("aria-controls") || "").startsWith("review-")
  );

// Row 1: the genuine data-integrity error.
await act(async () => {
  reviewRowButtons()[0]?.click();
});
await settle(150);
check(
  "data-integrity flag gets the data-problem explanation",
  text().includes("a customer or merchant reference on this opportunity points at nothing")
);
check(
  "data-integrity flag does NOT get the judgement-call explanation",
  !text().includes("recovery likelihood was too uncertain to decide automatically")
);

// Row 2: the legitimate judgement call — close it first so both panels
// aren't open and bleeding into each other's text() check.
await act(async () => {
  reviewRowButtons()[0]?.click();
});
await settle(80);
await act(async () => {
  reviewRowButtons()[1]?.click();
});
await settle(150);
check(
  "judgement-call flag gets the judgement-call explanation",
  text().includes("recovery likelihood was too uncertain to decide automatically")
);
check(
  "judgement-call flag does NOT get the data-problem explanation",
  !text().includes("a customer or merchant reference on this opportunity points at nothing")
);

// Row 2 is still open: approve it. The row must leave the queue optimistically
// and the backend must receive POST /decisions/{id}/approve.
const approveBtn = [...document.querySelectorAll("button")].find((b) =>
  (b.textContent || "").includes("Approve & send")
);
check("approve button rendered on flagged row", !!approveBtn);
await act(async () => {
  approveBtn?.click();
});
await settle(150);
check(
  "approve POSTed to the backend",
  mutationCalls.some((c) => c === "approve:flag0002-judg-emen-tcal-l00000000002")
);
check(
  "approved row left the queue optimistically",
  !text().includes("EduVriksha Learning") && text().includes("GhostMerchant")
);

// Row 1 remains: dismiss it.
const reviewRowButtonsAfter = () =>
  [...document.querySelectorAll("button[aria-expanded]")].filter((b) =>
    (b.getAttribute("aria-controls") || "").startsWith("review-")
  );
await act(async () => {
  reviewRowButtonsAfter()[0]?.click();
});
await settle(150);
const dismissBtn = [...document.querySelectorAll("button")].find((b) =>
  (b.textContent || "").trim().startsWith("Dismiss")
);
check("dismiss button rendered on flagged row", !!dismissBtn);
await act(async () => {
  dismissBtn?.click();
});
await settle(150);
check(
  "reject POSTed to the backend",
  mutationCalls.some((c) => c === "reject:flag0001-int3-grty-erro-r00000000001")
);
check("queue back to the good empty state", text().includes("Nothing needs review"));

/* ── Result ──────────────────────────────────────────────────────────────── */

await act(async () => {
  root.unmount();
});
rmSync(outDir, { recursive: true, force: true });

console.log(
  `\n${failures.length === 0 ? "All checks passed." : `${failures.length} check(s) failed: ${failures.join(", ")}`}\n`
);
process.exit(failures.length === 0 ? 0 : 1);