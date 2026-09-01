import { Fragment, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Inbox,
  Search,
} from "lucide-react";
import { Card, EmptyState, ErrorState, IdChip, Pill, Skeleton } from "./primitives";
import DecisionPanel from "./DecisionPanel";
import { api } from "../api/client";
import { useApi } from "../hooks/useApi";
import { outcomeFor, rootCauseLabel } from "../lib/labels";
import { formatCount, formatDateTime, formatINR, formatRelative } from "../lib/format";

const FILTERS = [
  { id: "all", label: "All", status: undefined },
  { id: "dispatched", label: "Message sent", status: "dispatched" },
  { id: "skipped", label: "Held by guardrail", status: "skipped" },
  { id: "undecided", label: "Awaiting triage", status: "undecided" },
];

const PAGE_SIZES = [25, 50, 100];

function StatusCell({ status }) {
  const outcome = outcomeFor(status);
  return (
    <Pill tone={outcome.tone} className="w-fit" title={outcome.description}>
      {outcome.label}
    </Pill>
  );
}

function RowSkeleton({ columns }) {
  return (
    <tr className="border-b border-hairline">
      {Array.from({ length: columns }).map((_, i) => (
        <td key={i} className="px-4 py-3.5">
          <Skeleton className="h-4 w-full" />
        </td>
      ))}
    </tr>
  );
}

export default function ActivityFeed({ refreshToken, title, subtitle }) {
  const [filter, setFilter] = useState("all");
  const [limit, setLimit] = useState(25);
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState("");
  const [expandedId, setExpandedId] = useState(null);

  const status = FILTERS.find((f) => f.id === filter)?.status;

  const { data, loading, error, reload } = useApi(
    (opts) => api.opportunities({ status, limit, offset }, opts),
    [status, limit, offset, refreshToken]
  );

  // Changing the filter or page size invalidates the current offset — staying
  // on "page 4" of a different result set shows an arbitrary slice.
  useEffect(() => {
    setOffset(0);
    setExpandedId(null);
  }, [filter, limit, refreshToken]);

  const rows = data ?? [];

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((r) =>
      [r.merchant_name, r.root_cause, r.opportunity_id, r.customer_id, r.merchant_id]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(q))
    );
  }, [rows, query]);

  // The API returns a bare list with no total, so exact page counts aren't
  // knowable. A full page means "there is probably more"; a short page means
  // "this is the end". Say that rather than inventing a page count.
  const hasNext = rows.length === limit;
  const rangeStart = rows.length ? offset + 1 : 0;
  const rangeEnd = offset + rows.length;

  const toggleRow = (id) => setExpandedId((current) => (current === id ? null : id));

  return (
    <Card>
      <div className="flex flex-col gap-4 border-b border-hairline px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-[15px] font-semibold leading-tight text-ink-900">
              {title}
            </h2>
            <p className="mt-1 max-w-[68ch] text-[13px] leading-snug text-ink-400">
              {subtitle}
            </p>
          </div>

          <label className="relative w-full sm:w-64">
            <Search
              size={14}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-400"
              aria-hidden="true"
            />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter this page by merchant or ID"
              aria-label="Filter loaded rows"
              className="w-full rounded-md border border-hairline bg-white py-2 pl-8 pr-3 text-[13px] text-ink-900 placeholder:text-ink-400 focus:border-brand focus:outline-none"
            />
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setFilter(f.id)}
              aria-pressed={filter === f.id}
              className={`rounded-full px-3 py-1.5 text-xs font-semibold transition-colors ${
                filter === f.id
                  ? "bg-navy-900 text-white"
                  : "bg-canvas text-ink-600 hover:bg-slate-200/70"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <div className="p-5">
          <ErrorState error={error} onRetry={reload} />
        </div>
      ) : (
        <div className="overflow-x-auto scrollbar-slim">
          <table className="w-full min-w-[860px] border-collapse text-left">
            <thead>
              <tr className="border-b border-hairline bg-canvas/60">
                <th scope="col" className="w-10 px-4 py-2.5">
                  <span className="sr-only">Expand</span>
                </th>
                <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-ink-600">
                  Outcome
                </th>
                <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-ink-600">
                  Merchant
                </th>
                <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-ink-600">
                  Root cause
                </th>
                <th scope="col" className="px-4 py-2.5 text-right text-xs font-semibold text-ink-600">
                  At risk
                </th>
                <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-ink-600">
                  Customer
                </th>
                <th scope="col" className="px-4 py-2.5 text-xs font-semibold text-ink-600">
                  Raised
                </th>
              </tr>
            </thead>

            <tbody>
              {loading ? (
                Array.from({ length: 6 }).map((_, i) => <RowSkeleton key={i} columns={7} />)
              ) : visible.length === 0 ? (
                <tr>
                  <td colSpan={7}>
                    <EmptyState
                      icon={Inbox}
                      title={query ? "Nothing on this page matches" : "No opportunities here"}
                    >
                      {query
                        ? "The filter only searches rows already loaded. Clear it, or move to another page."
                        : "Try a different outcome filter, or check that the agent has run against this dataset."}
                    </EmptyState>
                  </td>
                </tr>
              ) : (
                visible.map((row) => {
                  const isOpen = expandedId === row.opportunity_id;
                  const panelId = `decision-${row.opportunity_id}`;
                  return (
                    <Fragment key={row.opportunity_id}>
                      <tr
                        onClick={() => toggleRow(row.opportunity_id)}
                        className={`cursor-pointer border-b border-hairline transition-colors ${
                          isOpen ? "bg-brand-50" : "hover:bg-canvas"
                        }`}
                      >
                        <td className="px-4 py-3">
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              toggleRow(row.opportunity_id);
                            }}
                            aria-expanded={isOpen}
                            aria-controls={panelId}
                            className="flex h-6 w-6 items-center justify-center rounded text-ink-400 hover:bg-white hover:text-ink-900"
                          >
                            <ChevronDown
                              size={15}
                              strokeWidth={2.25}
                              className={`transition-transform duration-150 ${
                                isOpen ? "rotate-180" : ""
                              }`}
                              aria-hidden="true"
                            />
                            <span className="sr-only">
                              {isOpen ? "Hide" : "Show"} the decision for{" "}
                              {row.merchant_name || "this opportunity"}
                            </span>
                          </button>
                        </td>
                        <td className="px-4 py-3">
                          <StatusCell status={row.status} />
                        </td>
                        <td className="px-4 py-3">
                          <span className="text-[13px] font-semibold text-ink-900">
                            {row.merchant_name || "Unknown merchant"}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-[13px] text-ink-600">
                          {rootCauseLabel(row.root_cause)}
                        </td>
                        <td className="px-4 py-3 text-right text-[13px] font-semibold text-ink-900">
                          {formatINR(row.amount_at_risk)}
                        </td>
                        <td className="px-4 py-3">
                          <IdChip value={row.customer_id} />
                        </td>
                        <td
                          className="px-4 py-3 text-[13px] text-ink-600"
                          title={formatDateTime(row.created_at)}
                        >
                          {formatRelative(row.created_at)}
                        </td>
                      </tr>

                      {isOpen ? (
                        <tr className="border-b border-hairline">
                          <td colSpan={7} className="bg-canvas p-0">
                            <div
                              id={panelId}
                              className="animate-expandDown border-l-2 border-brand px-5 py-6 sm:px-6"
                            >
                              <DecisionPanel opportunity={row} />
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-hairline px-5 py-3.5">
        <p className="text-xs text-ink-400">
          {loading
            ? "Loading…"
            : rows.length === 0
              ? "No rows on this page"
              : `Rows ${formatCount(rangeStart)}–${formatCount(rangeEnd)}${
                  query ? ` · ${formatCount(visible.length)} match the filter` : ""
                }`}
        </p>

        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-ink-400">
            Per page
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              className="rounded border border-hairline bg-white px-2 py-1.5 text-xs font-medium text-ink-900 focus:border-brand focus:outline-none"
            >
              {PAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>

          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setOffset((o) => Math.max(0, o - limit))}
              disabled={offset === 0 || loading}
              className="inline-flex items-center gap-1 rounded-md border border-hairline bg-white px-2.5 py-1.5 text-xs font-semibold text-ink-900 transition-colors hover:bg-canvas disabled:cursor-not-allowed disabled:text-ink-400 disabled:opacity-60"
            >
              <ChevronLeft size={13} strokeWidth={2.5} aria-hidden="true" />
              Previous
            </button>
            <button
              type="button"
              onClick={() => setOffset((o) => o + limit)}
              disabled={!hasNext || loading}
              className="inline-flex items-center gap-1 rounded-md border border-hairline bg-white px-2.5 py-1.5 text-xs font-semibold text-ink-900 transition-colors hover:bg-canvas disabled:cursor-not-allowed disabled:text-ink-400 disabled:opacity-60"
            >
              Next
              <ChevronRight size={13} strokeWidth={2.5} aria-hidden="true" />
            </button>
          </div>
        </div>
      </footer>
    </Card>
  );
}
