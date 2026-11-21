import { IndianRupee, PauseOctagon, Send, Sigma } from "lucide-react";
import { Card, ErrorState, Skeleton } from "./primitives";
import { useCountUp } from "../hooks/useCountUp";
import { formatCount, formatINR, formatINRCompact, formatPercent } from "../lib/format";

function MetricCard({ icon: Icon, label, value, format, footnote, accent }) {
  const animated = useCountUp(value ?? 0);
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] font-medium text-ink-600">{label}</p>
        <span
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${accent}`}
          aria-hidden="true"
        >
          <Icon size={15} strokeWidth={2.25} />
        </span>
      </div>
      <p className="mt-3 text-[28px] font-semibold leading-none tracking-[-0.02em] text-ink-900">
        {format(animated)}
      </p>
      <p className="mt-2.5 text-xs leading-snug text-ink-400">{footnote}</p>
    </Card>
  );
}

function MetricSkeleton() {
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <Skeleton className="h-4 w-28" />
        <Skeleton className="h-8 w-8 rounded-md" />
      </div>
      <Skeleton className="mt-3 h-7 w-32" />
      <Skeleton className="mt-3 h-3 w-40" />
    </Card>
  );
}

export default function StatsHeader({ stats, loading, error, onRetry }) {
  if (error) {
    return (
      <Card className="p-5">
        <ErrorState error={error} onRetry={onRetry} />
      </Card>
    );
  }

  if (loading || !stats) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricSkeleton />
        <MetricSkeleton />
        <MetricSkeleton />
        <MetricSkeleton />
      </div>
    );
  }

  const total = stats.total_decisions || 0;
  const share = (n) => (total ? formatPercent(n / total, 1) : "—");

  // dispatched + skipped does not equal total_decisions in this dataset
  // (3,428 + 3,659 against 10,166). The rest are decisions with no row in
  // recovery_agent_actions at all. Stating that plainly beats implying the
  // two headline numbers should add up.
  const unlogged = Math.max(0, total - (stats.dispatched || 0) - (stats.skipped || 0));

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        icon={Sigma}
        label="Decisions made"
        value={total}
        format={(v) => formatCount(Math.round(v))}
        accent="bg-brand-50 text-brand-700"
        footnote={
          unlogged > 0
            ? `${formatCount(unlogged)} have no dispatch outcome recorded yet`
            : "Every decision has a recorded outcome"
        }
      />
      <MetricCard
        icon={Send}
        label="Messages sent"
        value={stats.dispatched || 0}
        format={(v) => formatCount(Math.round(v))}
        accent="bg-brand-50 text-brand-700"
        footnote={`${share(stats.dispatched || 0)} of decisions · ${formatINRCompact(
          stats.total_amount_dispatched
        )} contacted about`}
      />
      <MetricCard
        icon={PauseOctagon}
        label="Held by guardrail"
        value={stats.skipped || 0}
        format={(v) => formatCount(Math.round(v))}
        accent="bg-slate-100 text-held"
        footnote={`${share(stats.skipped || 0)} of decisions · withheld on purpose, not failed`}
      />
      <MetricCard
        icon={IndianRupee}
        label="Amount at risk"
        value={stats.total_amount_at_risk || 0}
        format={(v) => formatINRCompact(v)}
        accent="bg-navy-900 text-white"
        footnote={`Estimated recovery (assumes 60%): ${formatINRCompact(0.6 * (stats.total_amount_at_risk || 0))}  |  Total: ${formatINR(stats.total_amount_at_risk)}`}
      />
    </div>
  );
}
