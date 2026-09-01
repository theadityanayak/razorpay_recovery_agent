import { useEffect, useState } from "react";
import { Clock3, GitBranch, ShieldAlert } from "lucide-react";
import PipelineStepper, { buildStages } from "./PipelineStepper";
import MessagePanel from "./MessagePanel";
import { ErrorState, Field, IdChip, Pill, Skeleton } from "./primitives";
import { resolveDecision } from "../api/decisionIndex";
import {
  actionLabel,
  isDivergent,
  riskLabel,
  rootCauseLabel,
} from "../lib/labels";
import { formatDateTime, formatINR, formatPercent } from "../lib/format";

function ConfidenceBar({ score }) {
  if (score === null || score === undefined) return <span className="text-ink-400">—</span>;
  const pct = Math.max(0, Math.min(1, score));
  // Confidence is deliberately monochrome. Colour-coding low confidence red
  // would read as "the agent got it wrong", when a low score on an
  // unrecoverable payment is the agent being correctly pessimistic.
  return (
    <span className="flex items-center gap-2">
      <span className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-200">
        <span
          className="block h-full rounded-full bg-navy-900"
          style={{ width: `${pct * 100}%` }}
        />
      </span>
      <span className="text-[13px] font-semibold tabular-nums">
        {formatPercent(pct, 0)}
      </span>
    </span>
  );
}

function PanelSkeleton() {
  return (
    <div className="space-y-5">
      <Skeleton className="h-14 w-full rounded-md" />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="space-y-2">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-4 w-28" />
          </div>
        ))}
      </div>
      <Skeleton className="h-24 w-full rounded-md" />
    </div>
  );
}

export default function DecisionPanel({ opportunity }) {
  const [state, setState] = useState({ decision: null, error: null, loading: true });

  useEffect(() => {
    let active = true;
    setState({ decision: null, error: null, loading: true });

    resolveDecision(opportunity.opportunity_id, opportunity.decision_id ?? null)
      .then((decision) => {
        if (active) setState({ decision, error: null, loading: false });
      })
      .catch((err) => {
        if (active) setState({ decision: null, error: err, loading: false });
      });

    return () => {
      active = false;
    };
  }, [opportunity.opportunity_id, opportunity.decision_id]);

  if (state.loading) return <PanelSkeleton />;
  if (state.error) return <ErrorState error={state.error} compact />;

  const decision = state.decision;

  if (!decision) {
    return (
      <div className="rounded-md border border-hairline bg-white p-4">
        <div className="flex items-start gap-3">
          <Clock3
            size={16}
            className="mt-0.5 shrink-0 text-ink-400"
            strokeWidth={2.25}
            aria-hidden="true"
          />
          <div>
            <p className="text-[13px] font-semibold text-ink-900">
              Not triaged yet
            </p>
            <p className="mt-1 max-w-[56ch] text-[13px] leading-relaxed text-ink-600">
              The agent hasn&apos;t reached this opportunity, so there is no
              decision to explain. It will appear here once the graph runs.
            </p>
          </div>
        </div>
      </div>
    );
  }

  const stages = buildStages({ decision, opportunity });
  const wasHeld = decision.action_status === "skipped" || decision.policy_check_result !== "passed";
  const divergent = isDivergent(opportunity.root_cause, decision.recommended_action);

  return (
    <div className="space-y-5">
      {decision.requires_human_review ? (
        <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-reviewBg p-3.5">
          <ShieldAlert
            size={16}
            className="mt-0.5 shrink-0 text-review"
            strokeWidth={2.25}
            aria-hidden="true"
          />
          <div>
            <p className="text-[13px] font-semibold text-ink-900">
              Flagged for a person
            </p>
            <p className="mt-1 max-w-[62ch] text-[13px] leading-relaxed text-ink-600">
              The agent stopped rather than acting on its own. Flags in this
              dataset are usually data-integrity problems — a customer or
              merchant reference that points at nothing — rather than close
              judgement calls.
            </p>
          </div>
        </div>
      ) : null}

      {/* The pipeline is the anchor of this panel: it answers "what did the
          agent do, in order" before any individual field is read. */}
      <div className="rounded-md border border-hairline bg-white px-4 py-5 sm:px-6">
        <PipelineStepper stages={stages} />
      </div>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-5">
        <Field label="Recovery outlook">{riskLabel(decision.risk_assessment)}</Field>
        <Field label="Confidence">
          <ConfidenceBar score={decision.confidence_score} />
        </Field>
        <Field label="Chosen action">{actionLabel(decision.recommended_action)}</Field>
        <Field label="Policy check">
          {decision.policy_check_result === "passed" ? (
            <Pill tone="positive">Passed</Pill>
          ) : (
            <Pill tone="held">{decision.policy_check_result || "Not recorded"}</Pill>
          )}
        </Field>
        <Field label="Decided">{formatDateTime(decision.decided_at)}</Field>
      </dl>

      {decision.reasoning_summary ? (
        <div>
          <p className="text-2xs font-medium text-ink-400">Agent reasoning</p>
          <p className="mt-1.5 max-w-[76ch] rounded-md bg-canvas px-3.5 py-3 font-mono text-xs leading-relaxed text-ink-600">
            {decision.reasoning_summary}
          </p>
        </div>
      ) : null}

      {divergent ? (
        <div className="flex items-start gap-3 rounded-md border border-hairline bg-white p-3.5">
          <GitBranch
            size={15}
            className="mt-0.5 shrink-0 text-ink-400"
            strokeWidth={2.25}
            aria-hidden="true"
          />
          <p className="max-w-[70ch] text-[13px] leading-relaxed text-ink-600">
            The risk event says{" "}
            <span className="font-semibold text-ink-900">
              {rootCauseLabel(opportunity.root_cause)}
            </span>
            , but the agent chose{" "}
            <span className="font-semibold text-ink-900">
              {actionLabel(decision.recommended_action)}
            </span>
            . That&apos;s expected: triage prefers the real gateway failure code
            over the event&apos;s stated cause when both are present.
          </p>
        </div>
      ) : null}

      {decision.action_failure_reason ? (
        <Field label="Dispatch note">{decision.action_failure_reason}</Field>
      ) : null}

      <div className="border-t border-hairline pt-5">
        <p className="mb-3 text-[13px] font-semibold text-ink-900">
          Message to the customer
        </p>
        <MessagePanel decisionId={decision.decision_id} wasHeld={wasHeld} />
      </div>

      <dl className="grid grid-cols-1 gap-x-6 gap-y-3 border-t border-hairline pt-4 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Decision ID">
          <IdChip value={decision.decision_id} truncate={13} />
        </Field>
        <Field label="Opportunity ID">
          <IdChip value={opportunity.opportunity_id} truncate={13} />
        </Field>
        <Field label="Customer">
          <IdChip value={decision.customer_id || opportunity.customer_id} truncate={13} />
        </Field>
        <Field label="Amount at risk">
          {formatINR(decision.amount_at_risk ?? opportunity.amount_at_risk)}
        </Field>
      </dl>
    </div>
  );
}
