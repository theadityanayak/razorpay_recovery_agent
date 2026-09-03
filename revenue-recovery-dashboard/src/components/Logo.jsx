/**
 * Brand lockup for the sidebar.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * PLACEHOLDER MARK — READ BEFORE SHIPPING
 *
 * `RecoveryMark` below is an ORIGINAL geometric glyph, not Razorpay's
 * trademark. It borrows the brand blue and the rounded-square container
 * convention so the lockup sits correctly in the layout, and nothing else.
 * Swap in the official SVG from Razorpay's brand assets before this goes in
 * front of anyone, assuming you have the rights to use it — dropping it into
 * `<RecoveryMark>` needs no other change, since every size in this file is
 * driven off the wrapper.
 * ─────────────────────────────────────────────────────────────────────────
 *
 * The lockup follows the product-suite pattern: parent brand carries the
 * weight and the colour, product name sits underneath in a lighter, muted
 * cut. Reversing that — a loud product name over a faint parent — is what
 * makes an internal tool look like it was built outside the company.
 */

/** The container glyph. Original artwork, standing in for the real mark. */
export function RecoveryMark({ className = "h-9 w-9" }) {
  return (
    <span
      className={`relative inline-flex shrink-0 items-center justify-center rounded-[11px] bg-brand shadow-pill ${className}`}
      aria-hidden="true"
    >
      <svg viewBox="0 0 24 24" className="h-[55%] w-[55%]" fill="none">
        {/* An ascending stroke that dips before it climbs: a payment that
            failed and then recovered. One idea, three segments, no gradient. */}
        <path
          d="M3.5 15.5L8 19l4.5-8 3 5L20.5 5"
          stroke="#fff"
          strokeWidth="2.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

/**
 * @param {{ compact?: boolean, className?: string }} props
 *   `compact` renders the mark alone, for collapsed or very narrow rails.
 */
export default function Logo({ compact = false, className = "" }) {
  return (
    <span className={`flex min-w-0 items-center gap-2.5 ${className}`}>
      <RecoveryMark />
      {compact ? null : (
        <span className="flex min-w-0 flex-col leading-none">
          <span className="truncate text-[15px] font-extrabold tracking-tight text-navy-900">
            Razorpay
          </span>
          <span className="mt-1 truncate text-[13px] font-medium leading-none text-ink-400">
            Recovery Agent
          </span>
        </span>
      )}
      <span className="sr-only">Razorpay Recovery Agent</span>
    </span>
  );
}
