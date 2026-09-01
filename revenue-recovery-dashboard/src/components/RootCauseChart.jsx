import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Info } from "lucide-react";
import { Card, CardHeader, ErrorState, Skeleton } from "./primitives";
import { api } from "../api/client";
import { rootCauseLabel } from "../lib/labels";
import { formatCount, formatINR, formatINRCompact } from "../lib/format";

const PAGE = 200; // main.py caps /opportunities at le=200
const PAGES = 5; // 1,000 most recent opportunities — enough for a stable shape

/**
 * /stats/summary has no root-cause breakdown, so this samples the most recent
 * opportunities and aggregates `root_cause` in the browser.
 *
 * The sample size is stated on the card. Charting 1,000 rows and labelling it
 * as if it covered all 10,166 decisions would be a quietly wrong number, which
 * is worse in a compliance tool than an obviously partial one.
 *
 * backend-patch.py adds GET /stats/root-causes, which aggregates in SQL over
 * the whole table. This tries that first and falls back on 404.
 */
function useRootCauses(refreshToken) {
  const [state, setState] = useState({ rows: [], sampled: 0, exact: false, loading: true, error: null });
  const tokenRef = useRef(0);

  useEffect(() => {
    const runId = ++tokenRef.current;
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));

    (async () => {
      try {
        const exact = await api.rootCauses();
        if (cancelled || runId !== tokenRef.current) return;
        setState({
          rows: exact.map((r) => ({
            key: r.root_cause,
            label: rootCauseLabel(r.root_cause),
            count: r.count,
            amount: r.amount_at_risk ?? 0,
          })),
          sampled: exact.reduce((sum, r) => sum + (r.count || 0), 0),
          exact: true,
          loading: false,
          error: null,
        });
      } catch (patchErr) {
        if (cancelled || runId !== tokenRef.current) return;
        if (patchErr?.isNetwork) {
          setState({ rows: [], sampled: 0, exact: false, loading: false, error: patchErr });
          return;
        }
        try {
          const pages = await Promise.all(
            Array.from({ length: PAGES }, (_, i) =>
              api.opportunities({ limit: PAGE, offset: i * PAGE })
            )
          );
          if (cancelled || runId !== tokenRef.current) return;

          const rows = pages.flat();
          const grouped = new Map();
          for (const row of rows) {
            const key = row.root_cause || "unclassified";
            const bucket = grouped.get(key) || { key, count: 0, amount: 0 };
            bucket.count += 1;
            bucket.amount += row.amount_at_risk || 0;
            grouped.set(key, bucket);
          }

          setState({
            rows: [...grouped.values()].map((b) => ({
              ...b,
              label: rootCauseLabel(b.key),
            })),
            sampled: rows.length,
            exact: false,
            loading: false,
            error: null,
          });
        } catch (err) {
          if (!cancelled && runId === tokenRef.current) {
            setState({ rows: [], sampled: 0, exact: false, loading: false, error: err });
          }
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [refreshToken]);

  return state;
}

function ChartTooltip({ active, payload, metric }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded-md border border-hairline bg-white px-3 py-2 shadow-pop">
      <p className="text-[13px] font-semibold text-ink-900">{d.label}</p>
      <p className="mt-1 text-xs text-ink-600">
        {formatCount(d.count)} {d.count === 1 ? "opportunity" : "opportunities"}
      </p>
      <p className="text-xs text-ink-600">{formatINR(d.amount)} at risk</p>
      {metric === "amount" ? null : null}
    </div>
  );
}

export default function RootCauseChart({ refreshToken }) {
  const [metric, setMetric] = useState("count");
  const { rows, sampled, exact, loading, error } = useRootCauses(refreshToken);

  const data = useMemo(() => {
    const key = metric === "count" ? "count" : "amount";
    return [...rows]
      .sort((a, b) => b[key] - a[key])
      .slice(0, 9)
      .map((r) => ({ ...r, value: r[key] }));
  }, [rows, metric]);

  const toggle = (
    <div className="flex rounded-md border border-hairline p-0.5" role="group" aria-label="Measure">
      {[
        { id: "count", label: "Volume" },
        { id: "amount", label: "Value" },
      ].map((opt) => (
        <button
          key={opt.id}
          type="button"
          onClick={() => setMetric(opt.id)}
          aria-pressed={metric === opt.id}
          className={`rounded px-2.5 py-1 text-xs font-semibold transition-colors ${
            metric === opt.id
              ? "bg-navy-900 text-white"
              : "text-ink-600 hover:bg-slate-50"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );

  return (
    <Card className="flex flex-col">
      <CardHeader
        title="Why payments failed"
        subtitle={
          exact
            ? "Every opportunity in the dataset, grouped by root cause."
            : `Root cause across the ${formatCount(sampled)} most recent opportunities.`
        }
        actions={toggle}
      />

      <div className="flex-1 p-5 pt-4">
        {loading ? (
          <div className="space-y-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="flex items-center gap-3">
                <Skeleton className="h-3 w-32" />
                <Skeleton className="h-4 flex-1" style={{ maxWidth: `${90 - i * 11}%` }} />
              </div>
            ))}
          </div>
        ) : error ? (
          <ErrorState error={error} compact />
        ) : data.length === 0 ? (
          <p className="py-10 text-center text-[13px] text-ink-400">
            No opportunities returned, so there is nothing to group yet.
          </p>
        ) : (
          <>
            <div style={{ height: Math.max(220, data.length * 38) }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  layout="vertical"
                  data={data}
                  margin={{ top: 0, right: 44, bottom: 0, left: 0 }}
                  barCategoryGap={10}
                >
                  <CartesianGrid horizontal={false} stroke="#EEF2F7" />
                  <XAxis
                    type="number"
                    tickLine={false}
                    axisLine={false}
                    tick={{ fill: "#8394A8", fontSize: 11 }}
                    tickFormatter={(v) =>
                      metric === "count" ? formatCount(v) : formatINRCompact(v)
                    }
                  />
                  <YAxis
                    type="category"
                    dataKey="label"
                    width={158}
                    tickLine={false}
                    axisLine={false}
                    tick={{ fill: "#4A5B70", fontSize: 12 }}
                  />
                  <Tooltip
                    cursor={{ fill: "#F4F6F9" }}
                    content={<ChartTooltip metric={metric} />}
                  />
                  <Bar dataKey="value" radius={[0, 3, 3, 0]} maxBarSize={20}>
                    {data.map((entry, i) => (
                      <Cell
                        key={entry.key}
                        // One hue, stepped down by rank. Distinct colours per
                        // category would imply a difference in kind that isn't
                        // in the data — these are all just failure causes.
                        fill={i === 0 ? "#0D94FB" : i < 3 ? "#4FB0FC" : "#A8D8FE"}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            {!exact ? (
              <p className="mt-3 flex items-start gap-1.5 border-t border-hairline pt-3 text-2xs leading-relaxed text-ink-400">
                <Info size={12} className="mt-px shrink-0" aria-hidden="true" />
                <span>
                  Sampled client-side because the API has no root-cause
                  aggregate. Apply backend-patch.py to chart the full table.
                </span>
              </p>
            ) : null}
          </>
        )}
      </div>
    </Card>
  );
}
