import { Activity, LayoutGrid, ShieldAlert, X } from "lucide-react";
import Logo from "./Logo";

/**
 * The nav labels are the product's vocabulary, so they're deliberately short
 * and concrete. The one-line hints that used to sit under each label are
 * gone: three items don't need explaining, and stacked two-line rows fight
 * the pill shape the active state depends on.
 */
const NAV = [
  { id: "overview", label: "Overview", icon: LayoutGrid },
  { id: "feed", label: "Activity", icon: Activity },
  { id: "review", label: "Needs review", icon: ShieldAlert },
];

export default function Sidebar({ view, onNavigate, reviewCount, open, onClose }) {
  return (
    <>
      {/* Scrim, mobile only */}
      <div
        className={`fixed inset-0 z-30 bg-navy-900/30 backdrop-blur-sm transition-opacity lg:hidden ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
        onClick={onClose}
        aria-hidden="true"
      />

      {/*
        White rail, no right border. On desktop it reads as part of the canvas
        rather than a separate panel, which is what keeps the eye on the cards.
        On mobile it becomes a floating sheet and picks up a shadow, because
        there it genuinely is above the content.
      */}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[248px] flex-col bg-surface shadow-float transition-transform duration-200 lg:translate-x-0 lg:bg-transparent lg:shadow-none ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
        aria-label="Sections"
      >
        <div className="flex items-center gap-3 px-6 pb-4 pt-7">
          <Logo />
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded-full p-1.5 text-ink-400 transition-colors hover:bg-subtle hover:text-ink-900 lg:hidden"
            aria-label="Close menu"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <nav className="mt-3 flex-1 space-y-1 px-4">
          {NAV.map(({ id, label, icon: Icon }) => {
            const active = view === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => onNavigate(id)}
                aria-current={active ? "page" : undefined}
                /*
                  Pill active state. The inactive rows carry no fill at all —
                  muted label, muted icon, and a barely-there hover — so the
                  one filled pill is unambiguous at a glance.
                */
                className={`flex w-full items-center gap-3 rounded-full px-4 py-2.5 text-left transition-all duration-150 ${
                  active
                    ? "bg-brand text-white shadow-pill"
                    : "text-ink-400 hover:bg-surface hover:text-ink-900 hover:shadow-soft"
                }`}
              >
                <Icon size={17} strokeWidth={2.25} aria-hidden="true" className="shrink-0" />
                <span className="min-w-0 flex-1 truncate text-[13.5px] font-bold">{label}</span>
                {id === "review" && reviewCount > 0 ? (
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-2xs font-bold ${
                      active ? "bg-white/25 text-white" : "bg-review-bg text-review"
                    }`}
                  >
                    {reviewCount}
                  </span>
                ) : null}
              </button>
            );
          })}
        </nav>

        {/*
          Footer note, kept because it's the one sentence that stops a new
          user misreading 3,659 held messages as 3,659 failures. It sits in a
          well rather than above a hairline — no borders anywhere in this rail.
        */}
        <div className="p-4">
          <p className="well px-4 py-3.5 text-2xs leading-relaxed text-ink-600">
            Messages held by the guardrail are the compliance rules working as
            intended, not failures.
          </p>
        </div>
      </aside>
    </>
  );
}
