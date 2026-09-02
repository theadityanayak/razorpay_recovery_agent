import { useState } from "react";
import { ChevronDown, ShieldCheck } from "lucide-react";
import { Card, CardHeader, EmptyState, ErrorState, FlaggedNotice, IdChip, Skeleton } from "./primitives";
import MessagePanel from "./MessagePanel";
import PipelineStepper, { buildStages } from "./PipelineStepper";
import { Field, Pill } from "./primitives";
import { api } from "../api/client";
import { useApi } from "../hooks/useApi";
import { actionLabel, riskLabel } from "../lib/labels";
import { formatDateTime, formatINR, formatPercent } from "../lib/format";

export default function ReviewQueue({ refreshToken, onBack }) {
  const [openId, setOpenId] = useState(null);

  const { data, loading, error, reload } = useApi(
    (opts) => api.decisions({ requiresHumanReview: true, limit: 50, offset: 0 }, opts),
    [refreshToken]
  );

  const rows = data ?? [];

  return (
    <Card>
      <CardHeader
        title="Decisions the agent flagged"
        subtitle="An empty queue is the good state — it means nothing needed a person. Most flags here are genuine judgement calls the agent wasn't confident enough to make on its own; a smaller number are data-integrity problems, like a customer or merchant reference that points at nothing."
        actions={
          onBack ? (
            <button
              type="button"
              onClick={onBack}
              className="rounded-md border border-hairline bg-white px-2.5 py-1.5 text-xs font-semibold text-ink-900 hover:bg-canvas"
            >
              Back to overview
            </button>
          ) : null
        }
      />

      {error ? (
        <div className="p-5">
          <ErrorState error={error} onRetry={reload} />
        </div>
      ) : loading ? (
        <div className="space-y-3 p-5">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full rounded-md" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState icon={ShieldCheck} title="Nothing needs review" tone="positive">
          Every decision the agent made was resolved within policy. Flagged
          decisions will appear here as soon as one is raised.
        </EmptyState>
      ) : (
        <ul className="divide-y divide-hairline">
          {rows.map((decision) => {
            const isOpen = openId === decision.decision_id;
            const panelId = `review-${decision.decision_id}`;
            return (
              <li key={decision.decision_id}>
                <button
                  type="button"
                  onClick={() =>
                    setOpenId((cur) => (cur === decision.decision_id ? null : decision.decision_id))
                  }
                  aria-expanded={isOpen}
                  aria-controls={panelId}
                  className={`flex w-full items-center gap-4 px-5 py-4 text-left transition-colors ${
                    isOpen ? "bg-reviewBg" : "hover:bg-canvas"
                  }`}
                >
                  <span className="h-8 w-1 shrink-0 rounded-full bg-review" aria-hidden="true" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-ink-900">
                      {decision.merchant_name || "Unknown merchant"}
                    </span>
                    <span className="mt-0.5 block truncate text-xs text-ink-400">
                      {decision.reasoning_summary || "No reasoning recorded"}
                    </span>
                  </span>
                  <span className="hidden shrink-0 text-[13px] font-semibold text-ink-900 sm:block">
                    {formatINR(decision.amount_at_risk)}
                  </span>
                  <Pill tone="review" className="hidden shrink-0 md:inline-flex">
                    {actionLabel(decision.recommended_action)}
                  </Pill>
                  <ChevronDown
                    size={16}
                    strokeWidth={2.25}
                    className={`shrink-0 text-ink-400 transition-transform duration-150 ${
                      isOpen ? "rotate-180" : ""
                    }`}
                    aria-hidden="true"
                  />
                </button>

                {isOpen ? (
                  <div
                    id={panelId}
                    className="animate-expandDown space-y-5 border-l-2 border-review bg-canvas px-5 py-6 sm:px-6"
                  >
                    <FlaggedNotice recommendedAction={decision.recommended_action} />

                    <div className="rounded-md border border-hairline bg-white px-4 py-5 sm:px-6">
                      <PipelineStepper stages={buildStages({ decision, opportunity: null })} />
                    </div>

                    <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-5">
                      <Field label="Recovery outlook">{riskLabel(decision.risk_assessment)}</Field>
                      <Field label="Confidence">
                        {formatPercent(decision.confidence_score ?? 0, 0)}
                      </Field>
                      <Field label="Chosen action">{actionLabel(decision.recommended_action)}</Field>
                      <Field label="Policy check">{decision.policy_check_result || "—"}</Field>
                      <Field label="Decided">{formatDateTime(decision.decided_at)}</Field>
                    </dl>

                    {decision.action_failure_reason ? (
                      <Field label="Why it stopped">{decision.action_failure_reason}</Field>
                    ) : null}

                    <div className="border-t border-hairline pt-5">
                      <p className="mb-3 text-[13px] font-semibold text-ink-900">
                        Message to the customer
                      </p>
                      <MessagePanel
                        decisionId={decision.decision_id}
                        wasHeld={decision.policy_check_result !== "passed"}
                      />
                    </div>

                    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 border-t border-hairline pt-4 sm:grid-cols-3">
                      <Field label="Decision ID">
                        <IdChip value={decision.decision_id} truncate={13} />
                      </Field>
                      <Field label="Customer">
                        <IdChip value={decision.customer_id} truncate={13} />
                      </Field>
                      <Field label="Merchant">
                        <IdChip value={decision.merchant_id} truncate={13} />
                      </Field>
                    </dl>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
