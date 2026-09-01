# Revenue Recovery — Agent Operations

Frontend for the AI Revenue Recovery Platform. Reads the FastAPI backend in
`main.py` and shows what the agent saw, what it decided, and what it sent.

React 18 · Vite 5 · Tailwind 3 · Recharts 2 · lucide-react

---

## Run it

Backend first, on port 8000:

```bash
pip install fastapi uvicorn
uvicorn main:app --reload --port 8000
```

Then the frontend, on port 5173:

```bash
npm install
npm run dev
```

Open http://localhost:5173.

**Port 5173 is not optional.** `main.py` sets `allow_origins` to an exact-match
list containing only `http://localhost:5173` and `http://127.0.0.1:5173`. Vite
is configured with `strictPort: true`, so if something else holds 5173 the dev
server refuses to start rather than quietly moving to 5174 and leaving every
request blocked by CORS with no visible cause. Free the port, or add the new
origin to `allow_origins`.

To point at a different backend, copy `.env.example` to `.env` and set
`VITE_API_BASE_URL`.

| Command | What it does |
| --- | --- |
| `npm run dev` | Dev server on 5173 |
| `npm run build` | Production build to `dist/` |
| `npm run preview` | Serve the build on 5173 |
| `npm run smoke` | Headless render test against a mocked API (no browser needed) |

---

## Two gaps in the API, and what I did about them

Both are handled in the frontend, so **the dashboard runs correctly against
`main.py` exactly as you sent it**. `backend-patch.py` contains optional,
additive changes that make it faster and one chart exact. Nothing in the patch
alters an existing field, so it can't break a consumer.

### 1. `/opportunities` doesn't return `decision_id`

The expandable row needs the decision behind an opportunity, and both
`/decisions/{id}` and `/messages/{id}` are keyed by `decision_id`.
`list_opportunities` already `SELECT`s `rad.decision_id` and branches on it to
compute `status` — it just isn't a field on `OpportunitySummary`, so Pydantic
drops it before it reaches the client. There's no `/decisions?opportunity_id=`
filter either, so a row can't look itself up.

- **Fallback (works today):** `src/api/decisionIndex.js` pages `/decisions` and
  builds an `opportunity_id → decision` map in the browser. ~51 requests at the
  200-row cap, issued four at a time, built once per page load and only when a
  row is actually expanded. Full `DecisionDetail` objects are cached on the way
  past, so a warm index makes expansion cost one request for the message.
- **Recommended:** add the field. Two lines, in `backend-patch.py`. The
  frontend uses it automatically when present and skips the index entirely.

### 2. No root-cause aggregate

`/stats/summary` has no breakdown by `root_cause`, so the bar chart aggregates
the 1,000 most recent opportunities client-side and **says so on the card**.
Charting a sample while implying it covered all 10,166 decisions would be a
quietly wrong number, which is worse in a compliance tool than an obviously
partial one. `backend-patch.py` adds `GET /stats/root-causes`, which the chart
tries first and falls back from on 404 — no frontend change needed.

### Also worth knowing

`dispatched + skipped` is 7,087 against `total_decisions` of 10,166. The
remaining 3,079 decisions have no row in `recovery_agent_actions` at all. The
donut gives them their own neutral segment rather than folding them into
either outcome, and the first metric card names the number.

`list_opportunities` also `LEFT JOIN`s decisions without deduplicating, so an
opportunity carrying two decision rows would appear twice and drift the
`LIMIT`/`OFFSET` paging. Nothing in the current dataset triggers it; a
`GROUP BY ro.opportunity_id` closes it off.

---

## How the brief's framing shows up in the UI

- **Held is not failed.** There is no red in the outcome vocabulary anywhere —
  the smoke test asserts zero red-classed nodes. Skipped decisions read "Held
  by guardrail" in slate, the pipeline shows a filled slate node rather than a
  crossed-out one, and the copy says contact was withheld on purpose.
- **A 404 from `/messages/{id}` is an explanation, not an error.** It renders
  as "No message was drafted", with the reason.
- **`[heuristic]` is surfaced, not hidden.** The prefix is stripped from the
  tone text and shown as a separate "Inferred, not merchant-supplied" label,
  so nobody reads it as merchant-provided brand guidance.
- **`root_cause` vs `recommended_action` divergence is explained inline** when
  the two disagree, rather than leaving it looking like a misfire.
- **The review queue's empty state is the good state**, styled green, not as a
  blank screen waiting for content.
- **No 3D.** The visual budget went into the decision-chain panel: a five-stage
  pipeline stepper, the reasoning summary, and the drafted message with its
  personalisation rationale.

---

## Design

Razorpay-inspired, per the brief: `#0D94FB` for primary actions and accents,
`#012652` for the sidebar, `#192839` for deep chrome, stark white cards on a
`#F4F6F9` canvas. Tokens live in `tailwind.config.js`, named by role rather
than by hue, so the palette can be swapped without touching components.

Type is IBM Plex Sans, with IBM Plex Mono reserved for actual machine
identifiers — UUIDs and the raw reasoning string — where monospacing is doing a
job rather than decorating a label. Figures are tabular throughout, since every
number here is money or a count read down a column.

Money uses lakh and crore (`₹24.49 Cr`), not `244.9M`. A dashboard for Indian
fintech that reports in millions reads as a foreign import.

Motion is limited to two moments: the count-up when the stats land, and the
pipeline filling in when you expand a row. Both are switched off entirely
under `prefers-reduced-motion`.

The footer carries a line noting the palette is a visual reference and the
project isn't affiliated with Razorpay — worth keeping if this goes on GitHub
or into an interview demo.

---

## Layout

```
src/
├── api/
│   ├── client.js          fetch wrapper, ApiError with isNotFound / isNetwork
│   └── decisionIndex.js   opportunity → decision resolution (see gap 1)
├── hooks/
│   ├── useApi.js          loading/error/data + AbortController on dep change
│   └── useCountUp.js      eased counter, no-ops under reduced motion
├── lib/
│   ├── format.js          INR lakh/crore, timezone-safe dates, relative time
│   └── labels.js          API enums → human copy; outcome + divergence rules
├── components/
│   ├── Sidebar.jsx        navy nav, review badge
│   ├── TopBar.jsx         /health poll, refresh
│   ├── StatsHeader.jsx    the four metric cards
│   ├── RootCauseChart.jsx horizontal bars, volume/value toggle
│   ├── OutcomeDonut.jsx   three-segment donut, contact rate in the centre
│   ├── ActivityFeed.jsx   filters, search, paging, expandable rows
│   ├── DecisionPanel.jsx  the reasoning chain — the main screen
│   ├── PipelineStepper.jsx the five LangGraph stages
│   ├── MessagePanel.jsx   drafted copy, tone, 404 state
│   ├── ReviewQueue.jsx    requires_human_review
│   └── primitives.jsx     Card, Pill, Field, Skeleton, Empty/Error, IdChip
└── App.jsx                shell and view switching
```

No router — the brief asked for a single page, and view switching is three
lines of state.

---

## Testing

`npm run smoke` bundles the app, renders it in jsdom against a mock of the
documented API, and asserts on the resulting DOM. It covers both backend
configurations, the 404 message path, currency formatting, chart fallback, row
expansion and the review queue's empty state. No browser or database required,
so it runs in CI as-is.

---

## Before this goes anywhere public

- Tighten `allow_origins` in `main.py` to the deployed frontend origin.
- The API has no auth. Anyone who can reach it can read every customer ID,
  merchant name and drafted message in the dataset. Fine for a local demo,
  worth a sentence in your README if the repo is public.
- Set `VITE_API_BASE_URL` at build time; the default localhost value is baked
  into the bundle otherwise.
