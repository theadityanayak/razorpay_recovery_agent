import { useEffect, useState } from "react";
import { Cpu, MessageSquareText, PauseOctagon, Sparkles } from "lucide-react";
import { ErrorState, Pill, Skeleton } from "./primitives";
import { api } from "../api/client";
import { channelLabel, splitTone } from "../lib/labels";
import { formatDateTime } from "../lib/format";

/**
 * The drafted WhatsApp/SMS copy for one decision.
 *
 * A 404 from /messages/{id} is the expected result for any decision the
 * guardrail blocked — no message was ever drafted. It is rendered as an
 * explanation, not as an error: no red, no warning icon, no "failed".
 */
export default function MessagePanel({ decisionId, wasHeld }) {
  const [state, setState] = useState({ data: null, missing: false, error: null, loading: true });

  useEffect(() => {
    if (!decisionId) {
      setState({ data: null, missing: false, error: null, loading: false });
      return undefined;
    }

    const controller = new AbortController();
    let active = true;
    setState({ data: null, missing: false, error: null, loading: true });

    api
      .message(decisionId, { signal: controller.signal })
      .then((data) => {
        if (active) setState({ data, missing: false, error: null, loading: false });
      })
      .catch((err) => {
        if (!active || err?.name === "AbortError") return;
        if (err?.isNotFound) {
          setState({ data: null, missing: true, error: null, loading: false });
        } else {
          setState({ data: null, missing: false, error: err, loading: false });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [decisionId]);

  if (state.loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-4 w-28" />
        <Skeleton className="h-20 w-full rounded-lg" />
        <Skeleton className="h-3 w-3/4" />
      </div>
    );
  }

  if (state.error) return <ErrorState error={state.error} compact />;

  if (state.missing) {
    return (
      <div className="flex items-start gap-3 rounded-md border border-slate-200 bg-slate-50 p-4">
        <PauseOctagon
          size={16}
          className="mt-0.5 shrink-0 text-held"
          strokeWidth={2.25}
          aria-hidden="true"
        />
        <div>
          <p className="text-[13px] font-semibold text-ink-900">
            No message was drafted
          </p>
          <p className="mt-1 max-w-[56ch] text-[13px] leading-relaxed text-ink-600">
            {wasHeld
              ? "The guardrail withheld contact for this customer, so the agent never called the model. Nothing to draft is the correct outcome here."
              : "Nothing is stored against this decision — either contact was withheld, or the decision predates message generation."}
          </p>
        </div>
      </div>
    );
  }

  const msg = state.data;
  const tone = splitTone(msg.tone_used);

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone="brand" icon={MessageSquareText}>
          {channelLabel(msg.channel)}
        </Pill>
        {msg.model_used ? (
          <Pill tone="neutral" icon={Cpu}>
            {msg.model_used}
          </Pill>
        ) : null}
        <span className="text-2xs text-ink-400">
          Drafted {formatDateTime(msg.generated_at)}
        </span>
      </div>

      {/* Bubble, tail on the left. Flat fill, no gradient or drop shadow —
          this is a record of what was sent, not a chat mock-up. */}
      <div className="relative mt-3 rounded-lg rounded-tl-sm border border-brand-100 bg-brand-50 px-4 py-3.5">
        <p className="max-w-[68ch] whitespace-pre-wrap text-[14px] leading-relaxed text-ink-900">
          {msg.message_body || "The message body is empty."}
        </p>
      </div>

      <dl className="mt-4 space-y-3.5">
        {tone.text ? (
          <div>
            <dt className="flex items-center gap-1.5 text-2xs font-medium text-ink-400">
              Tone
              {tone.inferred ? (
                <span
                  className="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 text-2xs font-semibold text-held"
                  title="Inferred by the model from the merchant's industry and business type. The dataset holds no merchant-supplied brand voice."
                >
                  <Sparkles size={9} strokeWidth={2.5} aria-hidden="true" />
                  Inferred, not merchant-supplied
                </span>
              ) : null}
            </dt>
            <dd className="mt-1 max-w-[70ch] text-[13px] leading-relaxed text-ink-600">
              {tone.text}
            </dd>
          </div>
        ) : null}

        {msg.personalization_reasoning ? (
          <div>
            <dt className="text-2xs font-medium text-ink-400">
              Why it was written this way
            </dt>
            <dd className="mt-1 max-w-[70ch] text-[13px] leading-relaxed text-ink-600">
              {msg.personalization_reasoning}
            </dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}
