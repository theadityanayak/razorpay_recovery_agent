import { Activity, LayoutGrid, ShieldAlert, X } from "lucide-react";

const NAV = [
  {
    id: "overview",
    label: "Overview",
    icon: LayoutGrid,
    hint: "Volumes, causes and outcomes",
  },
  {
    id: "feed",
    label: "Activity",
    icon: Activity,
    hint: "Every recovery opportunity",
  },
  {
    id: "review",
    label: "Needs review",
    icon: ShieldAlert,
    hint: "Decisions the agent flagged",
  },
];

export default function Sidebar({ view, onNavigate, reviewCount, open, onClose }) {
  return (
    <>
      {/* Scrim, mobile only */}
      <div
        className={`fixed inset-0 z-30 bg-navy-900/40 transition-opacity lg:hidden ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
        onClick={onClose}
        aria-hidden="true"
      />

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col bg-navy-900 transition-transform duration-200 lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
        aria-label="Sections"
      >
        <div className="flex items-center gap-3 px-5 py-5">
          <span
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand"
            aria-hidden="true"
          >
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none">
              <path
                d="M5 17l4.5-9 2.5 5 1.8-2.6L19 17"
                stroke="#fff"
                strokeWidth="2.1"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold leading-tight text-white">
              Revenue Recovery
            </p>
            <p className="truncate text-xs leading-tight text-white/55">
              Agent operations
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded p-1 text-white/60 hover:bg-white/10 hover:text-white lg:hidden"
            aria-label="Close menu"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <nav className="mt-2 flex-1 space-y-1 px-3">
          {NAV.map(({ id, label, icon: Icon, hint }) => {
            const active = view === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => onNavigate(id)}
                aria-current={active ? "page" : undefined}
                className={`group flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left transition-colors ${
                  active
                    ? "bg-brand text-white"
                    : "text-white/70 hover:bg-white/[0.07] hover:text-white"
                }`}
              >
                <Icon size={17} strokeWidth={2} aria-hidden="true" className="shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-semibold leading-tight">
                    {label}
                  </span>
                  <span
                    className={`block truncate text-2xs leading-tight ${
                      active ? "text-white/75" : "text-white/40"
                    }`}
                  >
                    {hint}
                  </span>
                </span>
                {id === "review" && reviewCount > 0 ? (
                  <span
                    className={`shrink-0 rounded-full px-1.5 py-0.5 text-2xs font-semibold ${
                      active ? "bg-white/20 text-white" : "bg-review/20 text-amber-200"
                    }`}
                  >
                    {reviewCount}
                  </span>
                ) : null}
              </button>
            );
          })}
        </nav>

        <div className="border-t border-white/10 px-5 py-4">
          <p className="text-2xs leading-relaxed text-white/45">
            Withheld messages are the compliance guardrail working as intended,
            not failures.
          </p>
        </div>
      </aside>
    </>
  );
}
