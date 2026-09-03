import { useState } from "react";
import { AlertTriangle, Check, Copy, Info, RefreshCw, ShieldAlert } from "lucide-react";

/**
 * White card floating on the canvas. No border — separation is shadow and
 * space only. This is the top of the radius hierarchy: `rounded-3xl` here,
 * `rounded-2xl` for panels nested inside it, `rounded-xl` for controls.
 */
export function Card({ className = "", children, ...rest }) {
  return (
    <section className={`rounded-3xl bg-surface shadow-float ${className}`} {...rest}>
      {children}
    </section>
  );
}

/**
 * Card header. The rule that used to sit under every header is gone; the
 * padding below it does that job, and one less hairline per card is the
 * difference between "dense admin tool" and "console you don't mind reading".
 */
export function CardHeader({ title, subtitle, actions, className = "" }) {
  return (
    <header
      className={`flex flex-wrap items-start justify-between gap-4 px-7 pb-1 pt-7 ${className}`}
    >
      <div className="min-w-0">
        <h2 className="text-[17px] font-bold leading-tight tracking-tight text-ink-900">{title}</h2>
        {subtitle ? (
          <p className="mt-2 max-w-[62ch] text-[13px] leading-relaxed text-ink-600">{subtitle}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/* ── Badges ──────────────────────────────────────────────────────────────── */

const PILL_TONES = {
  brand: "bg-brand-50 text-brand-700",
  neutral: "bg-subtle text-ink-600",
  held: "bg-held-bg text-held",
  review: "bg-review-bg text-review",
  positive: "bg-positive-bg text-positive",
  // The only red in the product. Reserved for a hard policy block and for
  // genuine data faults — never for a guardrail hold, which is correct
  // behaviour and gets the calm slate `held` tone instead.
  block: "bg-block-bg text-block",
  dark: "bg-navy-900 text-white",
};

/**
 * Status badge. Fully round, no ring — the tinted fill carries the state on
 * its own, and rings on a badge this small read as noise at 13px.
 */
export function Pill({ tone = "neutral", icon: Icon, children, className = "", ...rest }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-semibold ${
        PILL_TONES[tone] ?? PILL_TONES.neutral
      } ${className}`}
      {...rest}
    >
      {Icon ? <Icon size={12} strokeWidth={2.5} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

/**
 * A parsed badge from `lib/reasoning`. Identical to `Pill` visually, but it
 * takes the badge object whole and hangs the plain-English explanation off
 * `title`, so the meaning is one hover away and never on screen as clutter.
 */
export function ReasonBadge({ badge, className = "" }) {
  if (!badge) return null;
  return (
    <Pill tone={badge.tone} title={badge.detail} className={className}>
      {badge.label}
    </Pill>
  );
}

/** Small label + value pair used throughout the decision detail. */
export function Field({ label, children, className = "" }) {
  return (
    <div className={className}>
      <dt className="text-2xs font-semibold text-ink-400">{label}</dt>
      <dd className="mt-1.5 text-[13px] font-semibold leading-snug text-ink-900">{children}</dd>
    </div>
  );
}

/**
 * "Why was this flagged" explanation, shared by every screen that shows a
 * requires_human_review decision (DecisionPanel and ReviewQueue). Living in
 * one place means fixing the copy fixes it everywhere it's shown, instead of
 * the two screens silently drifting apart.
 *
 * Most requires_human_review rows are legitimate judgement calls (recovery
 * likelihood too uncertain to decide automatically) inherited from the
 * original dataset — a much smaller number are genuine data-integrity errors
 * (a customer/merchant reference pointing at nothing) raised by the agent
 * itself. The copy differs because the two are not the same kind of problem.
 */
export function FlaggedNotice({ recommendedAction }) {
  const isDataIntegrityError = recommendedAction === "data_integrity_error";
  return (
    <div
      className={`flex items-start gap-3 rounded-2xl p-4 ${
        isDataIntegrityError ? "bg-block-bg" : "bg-review-bg"
      }`}
    >
      <ShieldAlert
        size={16}
        className={`mt-0.5 shrink-0 ${isDataIntegrityError ? "text-block" : "text-review"}`}
        strokeWidth={2.5}
        aria-hidden="true"
      />
      <div>
        <p className="text-[13px] font-bold text-ink-900">Flagged for a person</p>
        <p className="mt-1 max-w-[62ch] text-[13px] leading-relaxed text-ink-700">
          {isDataIntegrityError
            ? "The agent stopped because a customer or merchant reference on this opportunity points at nothing — a data problem, not a judgement call."
            : "The agent stopped rather than acting on its own. This is a genuine judgement call — recovery likelihood was too uncertain to decide automatically — not an error."}
        </p>
      </div>
    </div>
  );
}

/**
 * Loading placeholder.
 *
 * The default radius is applied ONLY when the caller hasn't set one. Tailwind
 * emits `.rounded-xl` after `.rounded-full` and `.rounded-2xl` in the
 * stylesheet, so a hard-coded default here would win over anything a caller
 * passed — which quietly turned the donut's circular skeleton into a rounded
 * square. Class order in the attribute means nothing; source order in the
 * sheet decides, so the only safe fix is not to emit the default at all.
 */
export function Skeleton({ className = "", ...rest }) {
  const hasRadius = /(?:^|\s)rounded(?:-|$|\s)/.test(className);
  return (
    <div
      className={`skeleton ${hasRadius ? "" : "rounded-xl"} ${className}`}
      aria-hidden="true"
      {...rest}
    />
  );
}

export function EmptyState({ icon: Icon, title, children, tone = "neutral" }) {
  const ring = tone === "positive" ? "bg-positive-bg text-positive" : "bg-subtle text-ink-400";
  return (
    <div className="flex flex-col items-center px-6 py-16 text-center">
      {Icon ? (
        <span className={`mb-4 flex h-12 w-12 items-center justify-center rounded-2xl ${ring}`}>
          <Icon size={20} strokeWidth={2} aria-hidden="true" />
        </span>
      ) : null}
      <p className="text-[15px] font-bold text-ink-900">{title}</p>
      {children ? (
        <p className="mt-2 max-w-[46ch] text-[13px] leading-relaxed text-ink-600">{children}</p>
      ) : null}
    </div>
  );
}

/**
 * Error state. Says what failed and what to do about it — a dashboard that
 * only says "Something went wrong" makes the reader guess between "backend
 * off", "wrong port" and "bad ID".
 */
export function ErrorState({ error, onRetry, compact = false }) {
  const message = error?.message || "The request did not complete.";
  return (
    <div
      className={`flex items-start gap-3 rounded-2xl bg-review-bg ${compact ? "p-4" : "p-5"}`}
      role="alert"
    >
      <AlertTriangle
        size={16}
        className="mt-0.5 shrink-0 text-review"
        strokeWidth={2.5}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-bold text-ink-900">
          {error?.isNetwork ? "No response from the API" : "The API returned an error"}
        </p>
        <p className="mt-1 text-[13px] leading-relaxed text-ink-700">{message}</p>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-surface px-3 py-1.5 text-xs font-bold text-ink-900 shadow-soft transition-shadow hover:shadow-lift"
          >
            <RefreshCw size={12} strokeWidth={2.5} aria-hidden="true" />
            Try again
          </button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Collapsed disclosure holding the untranslated backend string.
 *
 * Every machine value in this console is translated before a person sees it,
 * but the original still has to be reachable: when an ops user escalates a
 * row to engineering, the log line is the thing engineering asks for. So it
 * stays in the DOM, one click away, and never on screen by default.
 */
export function RawDisclosure({ label = "Raw agent output", children }) {
  return (
    <details className="group">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-full px-2 py-1 text-2xs font-semibold text-ink-400 transition-colors hover:bg-subtle hover:text-ink-600 [&::-webkit-details-marker]:hidden">
        <Info size={11} strokeWidth={2.5} aria-hidden="true" />
        {label}
      </summary>
      <p className="mt-2 max-w-[76ch] overflow-x-auto whitespace-pre-wrap break-words rounded-2xl bg-subtle px-4 py-3 font-mono text-2xs leading-relaxed text-ink-600">
        {children}
      </p>
    </details>
  );
}

/** Copies an identifier. UUIDs are unreadable but constantly needed. */
export function CopyButton({ value, label = "Copy" }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Clipboard API needs a secure context; fall back to a temp selection.
      const el = document.createElement("textarea");
      el.value = value;
      el.setAttribute("readonly", "");
      el.style.position = "fixed";
      el.style.opacity = "0";
      document.body.appendChild(el);
      el.select();
      try {
        document.execCommand("copy");
      } catch {
        /* nothing else to try */
      }
      document.body.removeChild(el);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  return (
    <button
      type="button"
      onClick={copy}
      title={copied ? "Copied" : `${label} ${value}`}
      aria-label={copied ? "Copied" : `${label} ${value}`}
      className="inline-flex items-center rounded-lg p-1 text-ink-300 transition-colors hover:bg-subtle hover:text-ink-900"
    >
      {copied ? (
        <Check size={12} strokeWidth={2.5} className="text-positive" aria-hidden="true" />
      ) : (
        <Copy size={12} strokeWidth={2} aria-hidden="true" />
      )}
    </button>
  );
}

/** Monospaced identifier with a copy affordance. */
export function IdChip({ value, truncate = 8 }) {
  if (!value) return <span className="text-ink-300">—</span>;
  return (
    <span className="inline-flex items-center gap-0.5">
      <code className="font-mono text-xs font-normal text-ink-600" title={value}>
        {String(value).slice(0, truncate)}
      </code>
      <CopyButton value={value} label="Copy full ID" />
    </span>
  );
}
