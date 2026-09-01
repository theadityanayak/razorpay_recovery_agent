import { useState } from "react";
import { AlertTriangle, Check, Copy, RefreshCw } from "lucide-react";

/** Stark white card on the light-gray canvas. */
export function Card({ className = "", children, ...rest }) {
  return (
    <section
      className={`rounded-lg border border-hairline bg-white shadow-card ${className}`}
      {...rest}
    >
      {children}
    </section>
  );
}

export function CardHeader({ title, subtitle, actions }) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3 border-b border-hairline px-5 py-4">
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold leading-tight text-ink-900">
          {title}
        </h2>
        {subtitle ? (
          <p className="mt-1 max-w-[62ch] text-[13px] leading-snug text-ink-400">
            {subtitle}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

const PILL_TONES = {
  brand: "bg-brand-50 text-brand-700 ring-brand-100",
  held: "bg-slate-100 text-held ring-slate-200",
  neutral: "bg-slate-50 text-ink-600 ring-hairline",
  review: "bg-reviewBg text-review ring-amber-200",
  positive: "bg-emerald-50 text-positive ring-emerald-200",
  dark: "bg-navy-700 text-white ring-navy-700",
};

/**
 * Status pill. Note there is no "danger" tone in this map — nothing the
 * agent legitimately decides is an error, so no outcome is ever styled red.
 * Genuine faults (a dangling foreign key) use `review` amber instead.
 */
export function Pill({ tone = "neutral", icon: Icon, children, className = "" }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${
        PILL_TONES[tone] ?? PILL_TONES.neutral
      } ${className}`}
    >
      {Icon ? <Icon size={12} strokeWidth={2.25} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

/** Small label + value pair used throughout the decision detail. */
export function Field({ label, children, className = "" }) {
  return (
    <div className={className}>
      <dt className="text-2xs font-medium text-ink-400">{label}</dt>
      <dd className="mt-1 text-[13px] font-medium leading-snug text-ink-900">
        {children}
      </dd>
    </div>
  );
}

export function Skeleton({ className = "" }) {
  return <div className={`skeleton rounded ${className}`} aria-hidden="true" />;
}

export function EmptyState({ icon: Icon, title, children, tone = "neutral" }) {
  const ring =
    tone === "positive"
      ? "bg-emerald-50 text-positive"
      : "bg-slate-100 text-ink-400";
  return (
    <div className="flex flex-col items-center px-6 py-14 text-center">
      {Icon ? (
        <span className={`mb-3 flex h-10 w-10 items-center justify-center rounded-full ${ring}`}>
          <Icon size={18} strokeWidth={2} aria-hidden="true" />
        </span>
      ) : null}
      <p className="text-sm font-semibold text-ink-900">{title}</p>
      {children ? (
        <p className="mt-1.5 max-w-[46ch] text-[13px] leading-relaxed text-ink-600">
          {children}
        </p>
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
      className={`flex items-start gap-3 rounded-md border border-amber-200 bg-reviewBg ${
        compact ? "p-3" : "p-4"
      }`}
      role="alert"
    >
      <AlertTriangle
        size={16}
        className="mt-0.5 shrink-0 text-review"
        strokeWidth={2.25}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-ink-900">
          {error?.isNetwork ? "No response from the API" : "The API returned an error"}
        </p>
        <p className="mt-1 text-[13px] leading-relaxed text-ink-600">{message}</p>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="mt-2.5 inline-flex items-center gap-1.5 rounded border border-hairline bg-white px-2.5 py-1.5 text-xs font-semibold text-ink-900 transition-colors hover:bg-slate-50"
          >
            <RefreshCw size={12} strokeWidth={2.25} aria-hidden="true" />
            Try again
          </button>
        ) : null}
      </div>
    </div>
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
      className="inline-flex items-center rounded p-1 text-ink-400 transition-colors hover:bg-slate-100 hover:text-ink-900"
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
  if (!value) return <span className="text-ink-400">—</span>;
  return (
    <span className="inline-flex items-center gap-0.5">
      <code className="font-mono text-xs text-ink-600" title={value}>
        {String(value).slice(0, truncate)}
      </code>
      <CopyButton value={value} label="Copy full ID" />
    </span>
  );
}
