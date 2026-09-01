import { useMemo } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { ShieldAlert } from "lucide-react";
import { Card, CardHeader, ErrorState, Skeleton } from "./primitives";
import { formatCount, formatINRCompact, formatPercent } from "../lib/format";

const COLORS = {
  dispatched: "#0D94FB",
  skipped: "#64809E",
  unlogged: "#D8E0EA",
};

function DonutTooltip({ active, payload, total }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded-md border border-hairline bg-white px-3 py-2 shadow-pop">
      <p className="text-[13px] font-semibold text-ink-900">{d.name}</p>
      <p className="mt-1 text-xs text-ink-600">
        {formatCount(d.value)} · {total ? formatPercent(d.value / total, 1) : "—"}
      </p>
      <p className="mt-1 max-w-[34ch] text-2xs leading-relaxed text-ink-400">{d.note}</p>
    </div>
  );
}

export default function OutcomeDonut({ stats, loading, error, onRetry, onShowReview }) {
  const { data, total, contactRate } = useMemo(() => {
    if (!stats) return { data: [], total: 0, contactRate: null };

    const dispatched = stats.dispatched || 0;
    const skipped = stats.skipped || 0;
    const totalDecisions = stats.total_decisions || 0;
    // Decisions with no row in recovery_agent_actions. Real in this dataset —
    // 10,166 decisions against 7,087 logged actions — so it gets its own
    // neutral segment rather than being folded into either outcome.
    const unlogged = Math.max(0, totalDecisions - dispatched - skipped);
    const decided = dispatched + skipped;

    return {
      total: totalDecisions,
      contactRate: decided ? dispatched / decided : null,
      data: [
        {
          key: "dispatched",
          name: "Message sent",
          value: dispatched,
          note: "The guardrail approved contact and a message was drafted and sent.",
        },
        {
          key: "skipped",
          name: "Held by guardrail",
          value: skipped,
          note: "Contact withheld on purpose — quiet hours, frequency caps or opt-outs.",
        },
        {
          key: "unlogged",
          name: "No outcome recorded",
          value: unlogged,
          note: "A decision exists but no dispatch action was logged against it.",
        },
      ].filter((d) => d.value > 0),
    };
  }, [stats]);

  const reviewCount = stats?.requires_human_review ?? 0;

  return (
    <Card className="flex flex-col">
      <CardHeader
        title="What happened to each decision"
        subtitle="Held is a correct outcome, so it reads as neutral here, not as a failure."
      />

      <div className="flex-1 p-5 pt-4">
        {error ? (
          <ErrorState error={error} onRetry={onRetry} compact />
        ) : loading || !stats ? (
          <div className="flex flex-col items-center gap-4 py-6">
            <Skeleton className="h-40 w-40 rounded-full" />
            <div className="w-full space-y-2">
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-4/5" />
              <Skeleton className="h-4 w-3/5" />
            </div>
          </div>
        ) : (
          <>
            <div className="relative mx-auto" style={{ height: 208, maxWidth: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={data}
                    dataKey="value"
                    nameKey="name"
                    innerRadius="66%"
                    outerRadius="94%"
                    paddingAngle={1.5}
                    startAngle={90}
                    endAngle={-270}
                    stroke="none"
                    isAnimationActive={false}
                  >
                    {data.map((entry) => (
                      <Cell key={entry.key} fill={COLORS[entry.key]} />
                    ))}
                  </Pie>
                  <Tooltip content={<DonutTooltip total={total} />} />
                </PieChart>
              </ResponsiveContainer>

              <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                <span className="text-[26px] font-semibold leading-none tracking-[-0.02em] text-ink-900">
                  {contactRate === null ? "—" : formatPercent(contactRate, 1)}
                </span>
                <span className="mt-1.5 max-w-[13ch] text-center text-2xs leading-tight text-ink-400">
                  of resolved decisions led to contact
                </span>
              </div>
            </div>

            <dl className="mt-5 space-y-2.5 border-t border-hairline pt-4">
              {data.map((d) => (
                <div key={d.key} className="flex items-center gap-2.5">
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-[3px]"
                    style={{ backgroundColor: COLORS[d.key] }}
                    aria-hidden="true"
                  />
                  <dt className="flex-1 text-[13px] text-ink-600">{d.name}</dt>
                  <dd className="text-[13px] font-semibold text-ink-900">
                    {formatCount(d.value)}
                    <span className="ml-1.5 text-xs font-normal text-ink-400">
                      {total ? formatPercent(d.value / total, 1) : ""}
                    </span>
                  </dd>
                </div>
              ))}
            </dl>

            <div className="mt-4 flex items-center justify-between gap-3 rounded-md bg-canvas px-3 py-2.5">
              <span className="text-xs text-ink-600">
                Value contacted about
              </span>
              <span className="text-[13px] font-semibold text-ink-900">
                {formatINRCompact(stats.total_amount_dispatched)}
              </span>
            </div>

            {reviewCount > 0 ? (
              <button
                type="button"
                onClick={onShowReview}
                className="mt-2.5 flex w-full items-center gap-2.5 rounded-md border border-amber-200 bg-reviewBg px-3 py-2.5 text-left transition-colors hover:bg-amber-50"
              >
                <ShieldAlert
                  size={15}
                  className="shrink-0 text-review"
                  strokeWidth={2.25}
                  aria-hidden="true"
                />
                <span className="flex-1 text-xs font-medium text-ink-900">
                  {formatCount(reviewCount)}{" "}
                  {reviewCount === 1 ? "decision needs" : "decisions need"} a
                  person to look
                </span>
                <span className="text-xs font-semibold text-review">Open</span>
              </button>
            ) : null}
          </>
        )}
      </div>
    </Card>
  );
}
