import { Check, Circle, Minus, Pause, ShieldAlert } from "lucide-react";

/**
 * The five LangGraph stages, in order.
 *
 * Numbering is used here because the content genuinely is a sequence — this
 * is the one place in the dashboard where step numbers carry information
 * rather than decoration.
 *
 * `held` is styled as a completed-but-different state: a filled slate node,
 * not a hollow or crossed-out one. The guardrail stopping the run is the
 * pipeline succeeding.
 */
const STATE_STYLES = {
  done: {
    node: "bg-brand text-white ring-brand",
    line: "bg-brand",
    label: "text-ink-900",
    icon: Check,
  },
  held: {
    node: "bg-held text-white ring-held",
    line: "bg-held",
    label: "text-ink-900",
    icon: Pause,
  },
  flagged: {
    node: "bg-review text-white ring-review",
    line: "bg-review",
    label: "text-ink-900",
    icon: ShieldAlert,
  },
  pending: {
    node: "bg-white text-ink-400 ring-hairline",
    line: "bg-hairline",
    label: "text-ink-400",
    icon: Circle,
  },
  skippedOver: {
    node: "bg-slate-100 text-ink-400 ring-hairline",
    line: "bg-hairline",
    label: "text-ink-400",
    icon: Minus,
  },
};

/**
 * Derives stage states from a decision + action row.
 * @returns {{key:string,label:string,detail:string,state:keyof STATE_STYLES}[]}
 */
export function buildStages({ decision, opportunity }) {
  const hasDecision = Boolean(decision);
  const policyPassed = decision?.policy_check_result === "passed";
  const flagged = Boolean(decision?.requires_human_review);
  const actionStatus = decision?.action_status;

  const dispatchState = flagged
    ? "flagged"
    : actionStatus === "success"
      ? "done"
      : actionStatus === "skipped"
        ? "held"
        : hasDecision
          ? "pending"
          : "pending";

  return [
    {
      key: "ingest",
      label: "Ingested",
      detail: opportunity?.root_cause
        ? "Risk event captured"
        : "Opportunity created",
      state: "done",
    },
    {
      key: "triage",
      label: "Triaged",
      detail: hasDecision ? "Root cause assessed" : "Not yet assessed",
      state: hasDecision ? "done" : "pending",
    },
    {
      key: "guardrail",
      label: "Guardrail",
      detail: !hasDecision
        ? "Waiting on triage"
        : policyPassed
          ? "Contact permitted"
          : "Contact withheld",
      state: !hasDecision ? "pending" : policyPassed ? "done" : "held",
    },
    {
      key: "strategy",
      label: "Strategy",
      detail: decision?.recommended_action
        ? "Action selected"
        : hasDecision
          ? "No action selected"
          : "Waiting on guardrail",
      state: decision?.recommended_action
        ? "done"
        : hasDecision
          ? "skippedOver"
          : "pending",
    },
    {
      key: "dispatch",
      label: "Dispatch",
      detail:
        dispatchState === "done"
          ? "Message sent"
          : dispatchState === "held"
            ? "Nothing sent, by design"
            : dispatchState === "flagged"
              ? "Stopped for review"
              : "No outcome recorded",
      state: dispatchState,
    },
  ];
}

export default function PipelineStepper({ stages }) {
  return (
    <ol className="flex items-start" aria-label="Agent pipeline">
      {stages.map((stage, i) => {
        const styles = STATE_STYLES[stage.state] ?? STATE_STYLES.pending;
        const Icon = styles.icon;
        const nextStyles = STATE_STYLES[stages[i + 1]?.state] ?? STATE_STYLES.pending;
        const isLast = i === stages.length - 1;

        return (
          <li
            key={stage.key}
            className="flex min-w-0 flex-1 flex-col items-center animate-pipelineIn"
            style={{ animationDelay: `${i * 55}ms` }}
          >
            <div className="flex w-full items-center" aria-hidden="true">
              <span className={`h-0.5 flex-1 ${i === 0 ? "bg-transparent" : styles.line}`} />
              <span
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-1 ${styles.node}`}
              >
                <Icon size={13} strokeWidth={2.75} />
              </span>
              <span
                className={`h-0.5 flex-1 ${isLast ? "bg-transparent" : nextStyles.line}`}
              />
            </div>
            <p
              className={`mt-2 text-center text-xs font-semibold leading-tight ${styles.label}`}
            >
              <span className="mr-1 font-mono text-2xs font-normal text-ink-400">
                {i + 1}
              </span>
              {stage.label}
            </p>
            <p className="mt-0.5 max-w-[15ch] text-center text-2xs leading-tight text-ink-400">
              {stage.detail}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
