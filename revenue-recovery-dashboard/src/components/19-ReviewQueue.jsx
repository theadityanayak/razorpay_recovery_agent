import { useState } from "react";
import { Check, ChevronDown, ShieldCheck, X } from "lucide-react";
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  FlaggedNotice,
  IdChip,
  RawDisclosure,
  ReasonBadge,
  Skeleton,
} from "./primitives";
import MessagePanel from "./MessagePanel";
import PipelineStepper, { buildStages } from "./PipelineStepper";
import { api } from "../api/client";
import { useApi } from "../hooks/useApi";
import { actionLabel } from "../lib/labels";
import { parseReasoning } from "../lib/reasoning";
import { formatDateTime, formatINR, formatPercent } from "../lib/format";

/**
 * The row summary used to print `reasoning_summary` verbatim, so an ops user
 * scanning this queue read:
 *
 *   Assessed as moderate_recovery_likelihood (p=0.46); action=escalate_to_human
 *
 * That is a log line. It's now parsed into badges by `lib/reasoning`, and the
 * probability is dropped from the row entirely — it survives in the expanded
 * panel, where there's room to say what it actually measures. The untouched
 * original is still one click away under "Raw agent output".
 */
function RowBadges({ decision, only }) {
  const { badges } = parseReasoning(decision.reasoning_summary, decision);
  // Select by id, never by slice. The collapsed row shows outlook + policy on
  // the left and the action on the right; slicing the first N would have put
  // the action badge in both places.
  const shown = only ? badges.filter((b) => only.includes(b.id)) : badges;
  if (shown.length === 0) return null;
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {shown.map((badge) => (
        <ReasonBadge key={badge.id} badge={badge} />
      ))}
    </span>
  );
}

/**
 * Confidence, shown as a meter rather than a bare "p=0.46".
 *
 * Deliberately monochrome: colour-coding a low score red would read as "the
 * agent got it wrong", when low confidence on an unrecoverable payment is the
 * agent being correctly pessimistic.
 */
function ConfidenceMeter({ score }) {
  if (score === null || score === undefined) return <span className="text-ink-300">Not scored</span>;
  const pct = Math.max(0, Math.min(1, score));
  return (
    <span
      className="flex items-center gap-2.5"
      title="How sure the agent was about its own assessment. It is a model score, not a probability that the payment will recover."
    >
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-subtle">
        <span className="block h-full rounded-full bg-navy-900" style={{ width: `${pct * 100}%` }} />
      </span>
      <span className="text-[13px] font-bold">{formatPercent(pct, 0)}</span>
    </span>
  );
}

export default function ReviewQueue({ refreshToken, onBack }) {
  const [openId, setOpenId] = useState(null);
  // decision_id -> "approved" | "dismissed": resolved rows drop out of the
  // queue immediately (optimistic), and come back with an error if the
  // backend rejects the call.
  const [resolved, setResolved] = useState({});
  const [pendingId, setPendingId] = useState(null);
  const [actionErrors, setActionErrors] = useState({});

  const { data, loading, error, reload } = useApi(
    (opts) => api.decisions({ requiresHumanReview: true, limit: 50, offset: 0 }, opts),
    [refreshToken]
  );

  const rows = (data ?? []).filter((d) => !resolved[d.decision_id]);

  async function resolveDecision(decision, action) {
    const id = decision.decision_id;
    const outcome = action === "approve" ? "approved" : "dismissed";
    setPendingId(id);
    setResolved((cur) => ({ ...cur, [id]: outcome })); // optimistic
    setOpenId((cur) => (cur === id ? null : cur));
    try {
      if (action === "approve") {
        await api.approveDecision(id);
      } else {
        await api.rejectDecision(id, null);
      }
    } catch (err) {
      // Roll the row back in and say why (e.g. 401 without an admin key,
      // 409 because someone else already resolved it).
      setResolved((cur) => {
        const next = { ...cur };
        delete next[id];
        return next;
      });
      setActionErrors((cur) => ({
        ...cur,
        [id]: err?.detail || err?.message || "The action failed.",
      }));
      setOpenId(id);
    } finally {
      setPendingId(null);
    }
  }

  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="Decisions the agent flagged"
        subtitle="An empty queue is the good state — it means nothing needed a person. Most flags here are judgement calls the agent wasn't confident enough to make alone; a smaller number are data problems, like a customer reference that points at nothing."
        actions={
          onBack ? (
            <button
              type="button"
              onClick={onBack}
              className="rounded-full bg-subtle px-4 py-2 text-xs font-bold text-ink-700 transition-colors hover:bg-brand-50 hover:text-brand-700"
            >
              Back to overview
            </button>
          ) : null
        }
      />

      {error ? (
        <div className="p-7 pt-5">
          <ErrorState error={error} onRetry={reload} />
        </div>
      ) : loading ? (
        <div className="space-y-3 p-7 pt-5">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[72px] w-full rounded-2xl" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState icon={ShieldCheck} title="Nothing needs review" tone="positive">
          Every decision the agent made was resolved within policy. Flagged
          decisions will appear here as soon as one is raised.
        </EmptyState>
      ) : (
        <ul className="space-y-2.5 p-4 pt-5 sm:p-5 sm:pt-5">
          {rows.map((decision) => {
            const isOpen = openId === decision.decision_id;
            const panelId = `review-${decision.decision_id}`;
            const parsed = parseReasoning(decision.reasoning_summary, decision);

            return (
              /*
                Each row is its own soft card rather than a divided list item.
                Dividers between rows in a queue this short add three hairlines
                and no information; a gap does the same job silently.
              */
              <li
                key={decision.decision_id}
                className={`overflow-hidden rounded-2xl transition-colors ${
                  isOpen ? "bg-subtle" : "bg-surface"
                }`}
              >
                <button
                  type="button"
                  onClick={() =>
                    setOpenId((cur) => (cur === decision.decision_id ? null : decision.decision_id))
                  }
                  aria-expanded={isOpen}
                  aria-controls={panelId}
                  className={`flex w-full items-center gap-4 rounded-2xl px-4 py-4 text-left transition-colors sm:px-5 ${
                    isOpen ? "bg-subtle" : "hover:bg-subtle"
                  }`}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[14px] font-bold text-ink-900">
                      {decision.merchant_name || "Unknown merchant"}
                    </span>
                    {/* Badges replace the raw string that used to sit here. */}
                    <span className="mt-2 flex flex-wrap items-center gap-1.5">
                      <RowBadges decision={decision} only={["outlook", "policy"]} />
                    </span>
                  </span>
                  <span className="hidden shrink-0 text-[14px] font-bold text-ink-900 sm:block">
                    {formatINR(decision.amount_at_risk)}
                  </span>
                  <span className="hidden shrink-0 md:block">
                    <RowBadges decision={decision} only={["action"]} />
                  </span>
                  <ChevronDown
                    size={16}
                    strokeWidth={2.5}
                    className={`shrink-0 text-ink-400 transition-transform duration-200 ${
                      isOpen ? "rotate-180" : ""
                    }`}
                    aria-hidden="true"
                  />
                </button>

                {isOpen ? (
                  <div
                    id={panelId}
                    className="animate-expandDown space-y-6 px-4 pb-6 pt-2 sm:px-5"
                  >
                    <FlaggedNotice recommendedAction={decision.recommended_action} />

                    {/* Approve & Send / Dismiss — the human decision the
                        backend is waiting on. Optimistic: the row leaves the
                        queue instantly and returns with an error on failure. */}
                    <div className="flex flex-wrap items-center gap-3 rounded-2xl bg-subtle px-4 py-3.5 sm:px-5">
                      <button
                        type="button"
                        disabled={pendingId === decision.decision_id}
                        onClick={() => resolveDecision(decision, "approve")}
                        className="inline-flex items-center gap-1.5 rounded-full bg-brand-600 px-4 py-2 text-xs font-bold text-white transition-colors hover:bg-brand-700 disabled:opacity-50"
                      >
                        <Check size={14} strokeWidth={3} aria-hidden="true" />
                        Approve &amp; send
                      </button>
                      <button
                        type="button"
                        disabled={pendingId === decision.decision_id}
                        onClick={() => resolveDecision(decision, "dismiss")}
                        className="inline-flex items-center gap-1.5 rounded-full bg-surface px-4 py-2 text-xs font-bold text-ink-700 shadow-soft transition-colors hover:bg-hairline disabled:opacity-50"
                      >
                        <X size={14} strokeWidth={3} aria-hidden="true" />
                        Dismiss
                      </button>
                      <span className="text-xs font-medium text-ink-400">
                        {pendingId === decision.decision_id
                          ? "Talking to the backend…"
                          : "Approving sends the drafted message through the normal dispatch path; dismissing holds all contact."}
                      </span>
                      {actionErrors[decision.decision_id] ? (
                        <span className="w-full text-xs font-bold text-review" role="alert">
                          Couldn’t resolve this decision: {actionErrors[decision.decision_id]}
                        </span>
                      ) : null}
                    </div>

                    <div className="rounded-2xl bg-surface px-5 py-6 shadow-soft sm:px-7">
                      <PipelineStepper stages={buildStages({ decision, opportunity: null })} />
                    </div>

                    {/* Full badge set, including the policy check. */}
                    <div className="flex flex-wrap items-center gap-2">
                      <RowBadges decision={decision} />
                    </div>

                    <dl className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3 lg:grid-cols-4">
                      <Field label="Confidence">
                        <ConfidenceMeter score={decision.confidence_score} />
                      </Field>
                      <Field label="Chosen action">{actionLabel(decision.recommended_action)}</Field>
                      <Field label="Decided">{formatDateTime(decision.decided_at)}</Field>
                      <Field label="Amount at risk">{formatINR(decision.amount_at_risk)}</Field>
                    </dl>

                    {/* Free-prose reasoning — a data fault the agent described
                        in a real sentence — is shown as written. Only the
                        structured log lines get parsed away. */}
                    {parsed.narrative ? (
                      <Field label="What the agent reported">
                        <span className="font-medium leading-relaxed text-ink-700">
                          {parsed.narrative}
                        </span>
                      </Field>
                    ) : null}

                    {decision.action_failure_reason &&
                    decision.action_failure_reason !== parsed.narrative ? (
                      <Field label="Why it stopped">{decision.action_failure_reason}</Field>
                    ) : null}

                    <div>
                      <p className="mb-3 text-[13px] font-bold text-ink-900">
                        Message to the customer
                      </p>
                      <MessagePanel
                        decisionId={decision.decision_id}
                        wasHeld={decision.policy_check_result !== "passed"}
                      />
                    </div>

                    <dl className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-3">
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

                    {parsed.raw ? <RawDisclosure>{parsed.raw}</RawDisclosure> : null}
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
