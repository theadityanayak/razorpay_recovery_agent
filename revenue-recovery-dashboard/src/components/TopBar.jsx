import { useEffect, useState } from "react";
import { Menu, RefreshCw } from "lucide-react";
import { API_BASE, api } from "../api/client";
import { formatRelative } from "../lib/format";

const TITLES = {
  overview: {
    title: "Overview",
    subtitle: "What the agent has seen, decided and sent.",
  },
  feed: {
    title: "Activity",
    subtitle: "Every recovery opportunity. Open a row for the full reasoning chain.",
  },
  review: {
    title: "Needs review",
    subtitle: "Decisions the agent would not complete on its own.",
  },
};

/** Polls /health so a dead backend is obvious before you read stale numbers. */
function useHealth(pollMs = 20000) {
  const [health, setHealth] = useState({ status: "checking", at: null });

  useEffect(() => {
    let cancelled = false;
    let timer;

    const check = async () => {
      try {
        const res = await api.health();
        if (!cancelled) setHealth({ status: "ok", at: res?.time ?? null });
      } catch {
        if (!cancelled) setHealth({ status: "down", at: null });
      }
      if (!cancelled) timer = setTimeout(check, pollMs);
    };

    check();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [pollMs]);

  return health;
}

export default function TopBar({ view, onOpenMenu, onRefresh, refreshedAt }) {
  const health = useHealth();
  const meta = TITLES[view] ?? TITLES.overview;

  const dot =
    health.status === "ok"
      ? "bg-positive"
      : health.status === "down"
        ? "bg-review"
        : "bg-ink-400";

  const healthLabel =
    health.status === "ok"
      ? "API connected"
      : health.status === "down"
        ? "API unreachable"
        : "Checking API";

  return (
    <header className="sticky top-0 z-20 border-b border-hairline bg-white/90 backdrop-blur">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3.5 sm:px-6">
        <button
          type="button"
          onClick={onOpenMenu}
          className="rounded-md border border-hairline p-1.5 text-ink-600 hover:bg-slate-50 lg:hidden"
          aria-label="Open menu"
        >
          <Menu size={18} aria-hidden="true" />
        </button>

        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold leading-tight tracking-[-0.01em] text-ink-900">
            {meta.title}
          </h1>
          <p className="truncate text-[13px] leading-tight text-ink-400">
            {meta.subtitle}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <span
            className="hidden items-center gap-2 rounded-full border border-hairline px-2.5 py-1.5 sm:inline-flex"
            title={`${healthLabel} · ${API_BASE}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden="true" />
            <span className="text-xs font-medium text-ink-600">{healthLabel}</span>
          </span>

          <button
            type="button"
            onClick={onRefresh}
            className="inline-flex items-center gap-2 rounded-md bg-brand px-3 py-2 text-[13px] font-semibold text-white shadow-sm transition-colors hover:bg-brand-600 active:bg-brand-700"
          >
            <RefreshCw size={14} strokeWidth={2.25} aria-hidden="true" />
            Refresh
          </button>
        </div>

        {refreshedAt ? (
          <p className="w-full text-2xs text-ink-400 sm:w-auto sm:basis-full sm:text-right">
            Data read {formatRelative(refreshedAt)}
          </p>
        ) : null}
      </div>
    </header>
  );
}
