/** @type {import('tailwindcss').Config} */

/**
 * Design tokens for the Recovery Agent console.
 *
 * Two rules hold the whole system together, and every component below obeys
 * them:
 *
 *   1. RADIUS ENCODES HIERARCHY. Page-level containers are `rounded-3xl`,
 *      panels nested inside them are `rounded-2xl`, controls are `rounded-xl`,
 *      and anything that reads as a token or a state is `rounded-full`. One
 *      radius on everything flattens the hierarchy and is the single fastest
 *      way to make a dashboard look templated.
 *
 *   2. ELEVATION REPLACES BORDERS. Nothing that floats has an outline. Depth
 *      comes from two stacked shadows — a tight contact shadow plus a wide,
 *      very low-opacity ambient one. Hairlines survive only inside tables,
 *      where column alignment genuinely needs a rule.
 */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Razorpay blue, kept as the one accent in the product. Every other
        // colour in this palette is either neutral or strictly semantic.
        brand: {
          DEFAULT: "#0D94FB",
          700: "#0A6CB8",
          600: "#0B80DC",
          500: "#0D94FB",
          200: "#A9D9FE",
          100: "#DBEEFF",
          50: "#F0F7FF",
        },
        // Razorpay's deep navy. Used for the wordmark and for high-emphasis
        // numerals, never as a large fill — a navy sidebar reads heavier
        // than the "floating white card" aesthetic this console is after.
        navy: {
          900: "#0B1B33",
          700: "#16305A",
          500: "#2C4B7C",
        },
        ink: {
          900: "#0B1B33", // headings, figures
          700: "#33455F", // body
          600: "#5A6B85", // secondary
          400: "#8D9BB0", // meta, muted nav
          300: "#AFBACA", // placeholder
        },
        canvas: "#F5F6F8", // app background — soft, very light, barely blue
        surface: "#FFFFFF", // every card
        subtle: "#F7F8FA", // wells and inset panels sitting on white
        hairline: "#EDF0F4", // table rules only; never a card outline

        // Semantic states. Each one has to earn its colour: a state gets a hue
        // only when it differs in KIND from its neighbours, not in degree.
        positive: { DEFAULT: "#0E8A64", bg: "#E9F7F1", ring: "#B9E6D5" },
        // Amber: a person needs to look. Not a failure.
        review: { DEFAULT: "#A96A00", bg: "#FDF4E4", ring: "#F0DCB4" },
        // Slate: the guardrail withheld contact on purpose. Working as
        // intended, so it stays cool and quiet.
        held: { DEFAULT: "#5C7794", bg: "#F1F4F8", ring: "#DAE2EC" },
        // Red is reserved for a hard policy block and for genuine data faults
        // — the only two states in this product where something is actually
        // wrong. Guardrail holds never reach it.
        block: { DEFAULT: "#C4453D", bg: "#FDF0EF", ring: "#F3CFCC" },
      },

      fontFamily: {
        sans: [
          "Plus Jakarta Sans",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        // Second family with one narrow job: opaque machine values (UUIDs,
        // raw agent output). Distinct enough from the UI face that a reader
        // knows at a glance they're looking at something not written for them.
        mono: ["IBM Plex Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },

      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },

      letterSpacing: {
        tightest: "-0.03em",
      },

      borderRadius: {
        "4xl": "2rem",
      },

      boxShadow: {
        // Resting card. Barely there — the canvas does most of the separating.
        soft: "0 1px 2px rgba(11, 27, 51, 0.03), 0 6px 16px -6px rgba(11, 27, 51, 0.06)",
        // Primary surfaces that should read as floating above the canvas.
        float:
          "0 1px 2px rgba(11, 27, 51, 0.03), 0 10px 28px -8px rgba(11, 27, 51, 0.07), 0 24px 56px -20px rgba(11, 27, 51, 0.06)",
        // Hover / focus lift on interactive cards.
        lift: "0 2px 6px rgba(11, 27, 51, 0.04), 0 16px 40px -12px rgba(11, 27, 51, 0.12)",
        // Active pill in the sidebar, and small primary buttons.
        pill: "0 1px 2px rgba(13, 148, 251, 0.24), 0 6px 16px -6px rgba(13, 148, 251, 0.42)",
        // Tooltips and popovers.
        pop: "0 8px 32px -8px rgba(11, 27, 51, 0.16), 0 2px 8px rgba(11, 27, 51, 0.06)",
      },

      keyframes: {
        expandDown: {
          from: { opacity: "0", transform: "translateY(-4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        pipelineIn: {
          from: { opacity: "0.25", transform: "scale(0.9)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        expandDown: "expandDown 200ms cubic-bezier(0.22, 1, 0.36, 1)",
        pipelineIn: "pipelineIn 320ms cubic-bezier(0.22, 1, 0.36, 1) both",
      },
    },
  },
  plugins: [],
};
