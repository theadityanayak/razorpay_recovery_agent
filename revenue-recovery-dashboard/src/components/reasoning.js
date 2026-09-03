/**
 * Turns the agent's machine-written reasoning string into something a person
 * can read.
 *
 * The backend writes one of two shapes into `reasoning_summary`:
 *
 *   structured   "Assessed as moderate_recovery_likelihood (p=0.46);
 *                 action=escalate_to_human; policy_check=blocked"
 *   free prose   "Dangling reference on opportunity 51fd…: customer_id=ffff…
 *                 not found."
 *
 * The first shape is a log line. It was never written for a merchant ops
 * user, and printing it verbatim is the single biggest reason this console
 * read as unfinished. `parseReasoning` splits it into typed badges; the
 * probability is dropped from the visible row and survives only in the
 * tooltip, because a bare "p=0.46" invites a reader to treat a model score
 * as a business fact.
 *
 * The second shape is left alone. It's already a sentence, so it's returned
 * as `narrative` and rendered as prose.
 *
 * Nothing here calls the API or touches component state — it's a pure
 * function over a string, which keeps it trivially testable and means the
 * data hooks stay exactly as they were.
 */

import { humanise, actionLabel } from "./labels";

/* ── Vocabulary ──────────────────────────────────────────────────────────── */

/**
 * Recovery-likelihood wording.
 *
 * NOTE FOR REVIEW: these are deliberately phrased as *confidence*, matching
 * the requested "Moderate Confidence" label. Strictly the field is a recovery
 * likelihood ("might recover"), and `p` is a separate model confidence — the
 * two can disagree. Flip the strings in this one map if you'd rather the
 * badge said "Might recover"; nothing else needs to change.
 */
const OUTLOOK = {
  high_recovery_likelihood: {
    label: "High confidence",
    tone: "positive",
    detail: "The agent rated this payment as likely to recover.",
  },
  medium_recovery_likelihood: {
    label: "Moderate confidence",
    tone: "review",
    detail: "The agent rated this payment as possible, but not likely, to recover.",
  },
  moderate_recovery_likelihood: {
    label: "Moderate confidence",
    tone: "review",
    detail: "The agent rated this payment as possible, but not likely, to recover.",
  },
  low_recovery_likelihood: {
    label: "Low confidence",
    tone: "held",
    detail: "The agent rated this payment as unlikely to recover.",
  },
  data_integrity_error: {
    label: "Data error",
    tone: "block",
    detail: "A record this decision depends on is missing, so nothing could be assessed.",
  },
};

/**
 * Policy-check wording.
 *
 * The distinction that matters: a *hold* and a *block* are not the same
 * event. `blocked_quiet_hours` and friends are the compliance guardrail
 * doing its job on thousands of rows a day — colouring those red would paint
 * routine, correct behaviour as failure. A bare `blocked` is a hard stop, and
 * that is the one policy state that reads red.
 */
const POLICY = {
  passed: {
    label: "Policy passed",
    tone: "positive",
    detail: "Contact was permitted, so the agent went ahead.",
  },
  blocked: {
    label: "Policy block",
    tone: "block",
    detail: "Policy stopped this outright. No message was drafted or sent.",
  },
  blocked_quiet_hours: {
    label: "Held: quiet hours",
    tone: "held",
    detail: "It was outside contactable hours in the customer's timezone.",
  },
  blocked_frequency_cap: {
    label: "Held: contact cap",
    tone: "held",
    detail: "This customer had already been contacted as often as policy allows.",
  },
  blocked_opt_out: {
    label: "Held: opted out",
    tone: "held",
    detail: "This customer has opted out of recovery messages.",
  },
  blocked_consent: {
    label: "Held: no consent",
    tone: "held",
    detail: "No contact consent is recorded for this customer.",
  },
};

/** Actions that mean "a person has to take this on" get amber, not neutral. */
const ESCALATING_ACTIONS = new Set(["escalate_to_human", "data_integrity_error"]);

/* ── Badge builders ──────────────────────────────────────────────────────── */

export function outlookBadge(value) {
  if (!value) return null;
  const known = OUTLOOK[value];
  return {
    id: "outlook",
    tone: known?.tone ?? "neutral",
    label: known?.label ?? humanise(value),
    detail: known?.detail ?? "Recovery outlook recorded by the agent.",
    raw: value,
  };
}

export function policyBadge(value) {
  if (!value) return null;
  const known = POLICY[value];
  if (known) {
    return { id: "policy", tone: known.tone, label: known.label, detail: known.detail, raw: value };
  }
  // Unknown guardrail reasons will land in the data before they land in this
  // file. Degrade to a readable hold rather than leaking the raw enum.
  if (/^blocked[_-]/i.test(value)) {
    return {
      id: "policy",
      tone: "held",
      label: `Held: ${humanise(value.replace(/^blocked[_-]/i, "")).toLowerCase()}`,
      detail: "The compliance guardrail withheld contact for this customer.",
      raw: value,
    };
  }
  return { id: "policy", tone: "neutral", label: humanise(value), detail: "Policy result recorded by the agent.", raw: value };
}

export function actionBadge(value) {
  if (!value) return null;
  return {
    id: "action",
    tone: ESCALATING_ACTIONS.has(value) ? "review" : "brand",
    label: actionLabel(value),
    detail: "The step the agent chose after triage.",
    raw: value,
  };
}

/* ── Parser ──────────────────────────────────────────────────────────────── */

const OUTLOOK_RE = /assessed\s+as\s+([a-z0-9_]+)/i;
const PROB_RE = /\(\s*p\s*=\s*([0-9.]+)\s*\)/i;
const ACTION_RE = /\baction\s*=\s*([a-z0-9_]+)/i;
const POLICY_RE = /\bpolicy_check\s*=\s*([a-z0-9_]+)/i;

/**
 * @param {string|null|undefined} summary  raw `reasoning_summary`
 * @param {object} [fallback]              decision fields to fill any gaps
 * @returns {{
 *   badges: Array<{id:string,tone:string,label:string,detail:string,raw:string}>,
 *   probability: number|null,
 *   narrative: string|null,
 *   raw: string,
 *   structured: boolean
 * }}
 */
export function parseReasoning(summary, fallback = {}) {
  const raw = typeof summary === "string" ? summary.trim() : "";

  const outlookRaw = OUTLOOK_RE.exec(raw)?.[1] ?? fallback.risk_assessment ?? null;
  const actionRaw = ACTION_RE.exec(raw)?.[1] ?? fallback.recommended_action ?? null;
  const policyRaw = POLICY_RE.exec(raw)?.[1] ?? fallback.policy_check_result ?? null;

  const probMatch = PROB_RE.exec(raw);
  const parsedProb = probMatch ? Number(probMatch[1]) : null;
  const probability =
    parsedProb !== null && Number.isFinite(parsedProb)
      ? parsedProb
      : typeof fallback.confidence_score === "number"
        ? fallback.confidence_score
        : null;

  // "Structured" means the string was a log line we fully understood. If it
  // wasn't, whatever the agent wrote is a real sentence and belongs on screen
  // as prose — swallowing it would hide the only explanation of a data fault.
  const structured = Boolean(OUTLOOK_RE.test(raw) || ACTION_RE.test(raw) || POLICY_RE.test(raw));

  const badges = [outlookBadge(outlookRaw), actionBadge(actionRaw), policyBadge(policyRaw)].filter(
    Boolean
  );

  // On a data-integrity fault the backend writes the same sentinel into both
  // `risk_assessment` and `recommended_action`, which rendered as "Data error"
  // sitting next to "Data integrity error". Drop any badge that repeats a
  // value already on the row — two badges saying one thing is worse than one.
  const seen = new Set();
  const deduped = badges.filter((b) => {
    if (seen.has(b.raw)) return false;
    seen.add(b.raw);
    return true;
  });

  return {
    badges: deduped,
    probability,
    narrative: structured ? null : raw || null,
    raw,
    structured,
  };
}
