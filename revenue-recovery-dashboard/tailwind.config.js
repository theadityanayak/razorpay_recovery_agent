/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Razorpay-inspired. Named by role, not by hue, so swapping the
        // palette later doesn't require touching every component.
        brand: {
          DEFAULT: "#0D94FB",
          600: "#0A7ED8",
          700: "#0869B4",
          100: "#D6ECFE",
          50: "#EFF7FF",
        },
        navy: {
          900: "#012652", // sidebar
          800: "#0A2F60",
          700: "#192839", // deep surface / expanded row chrome
        },
        ink: {
          900: "#0F1B2D", // primary text
          600: "#4A5B70", // secondary text
          400: "#8394A8", // tertiary / meta
        },
        canvas: "#F4F6F9", // page background
        hairline: "#E4E9F0", // borders
        // "Held by guardrail" is a legitimate outcome, not an error, so it
        // gets a calm slate-blue rather than anything in the red family.
        held: "#64809E",
        review: "#B87503", // needs-human-review amber (AA on white)
        reviewBg: "#FEF6E7",
        positive: "#0F8A65",
      },
      fontFamily: {
        sans: [
          "IBM Plex Sans",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        mono: ["IBM Plex Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      boxShadow: {
        card: "0 1px 2px rgba(15, 27, 45, 0.04), 0 1px 3px rgba(15, 27, 45, 0.06)",
        pop: "0 8px 24px rgba(15, 27, 45, 0.10)",
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
        expandDown: "expandDown 180ms ease-out",
        pipelineIn: "pipelineIn 320ms cubic-bezier(0.22, 1, 0.36, 1) both",
      },
    },
  },
  plugins: [],
};
