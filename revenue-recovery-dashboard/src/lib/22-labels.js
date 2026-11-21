/**
 * Domain vocabulary.
 *
 * The API speaks in snake_case enums. Everything a person reads on screen is
 * translated here, in one place, so the same value never appears as
 * "subscription_failure" on one screen and "Subscription Failure" on another.
 *
 * Unknown values degrade to a title-cased version of the raw string rather
 * than being hidden — new root causes will appear in the data before they
 * appear in this file, and a blank cell is worse than an unpolished label.
 */

export function humanise(value) {
  if (!value) return "—";
  return String(value)
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^./, (c) => c.toUpperCase());
}

const ROOT_CAUSE_LABELS = {
  card_decline: "Card declined",
  insufficient_funds: "Insufficient funds",
  upi_timeout: "UPI timeout",
  subscription_failure: "Subscription failure",
  mandate_expired: "Mandate expired",
  mandate_revoked: "Mandate revoked",
  payment_gateway_error: "Gateway error",
  network_error: "Network error",
  authentication_failure: "Authentication failed",
  expired_card: "Card expired",
  bank_downtime: "Bank downtime",
  npci_outage: "NPCI outage",
  checkout_abandonment: "Checkout abandoned",
  failed_payment: "Payment failed",
};

export const rootCauseLabel = (v) => ROOT_CAUSE_LABELS[v] ?? humanise(v);

const ACTION_LABELS = {
  retry_payment: "Retry payment",
  send_reminder: "Send reminder",
  offer_alternate_method: "Offer another payment method",
  request_mandate_reauth: "Ask for mandate re-authorisation",
  escalate_to_human: "Escalate to a person",
  no_action: "Take no action",
  wait_and_retry: "Wait, then retry",
};

export const actionLabel = (v) => ACTION_LABELS[v] ?? humanise(v);

const RISK_LABELS = {
  high_recovery_likelihood: "Likely to recover",
  medium_recovery_likelihood: "Might recover",
  low_recovery_likelihood: "Unlikely to recover",
};

export const riskLabel = (v) => RISK_LABELS[v] ?? humanise(v);

/**
 * Outcome semantics for an opportunity row.
 *
 * The one rule the whole design hangs on: a skipped decision is the
 * compliance guardrail doing its job — quiet hours, a contact-frequency cap,
 * an opt-out — not a failure. It gets neutral slate, never red, and the copy
 * never implies something went wrong.
 */
export const OUTCOME = {
  dispatched: {
    key: "dispatched",
    label: "Message sent",
    short: "Sent",
    tone: "brand",
    description: "The guardrail approved contact and the message went out.",
  },
  skipped: {
    key: "skipped",
    label: "Held by guardrail",
    short: "Held",
    tone: "held",
    description:
      "Contact was withheld on purpose — quiet hours, a contact-frequency cap, or an opt-out.",
  },
  undecided: {
    key: "undecided",
    label: "Awaiting triage",
    short: "Queued",
    tone: "neutral",
    description: "The agent hasn't reached this opportunity yet.",
  },
  delivery_unknown: {
    key: "delivery_unknown",
    label: "Delivery unknown",
    short: "Unknown",
    tone: "review",
    description:
      "The send was interrupted and the provider may still have delivered it. " +
      "Reconcile against the provider dashboard before any retry — the agent will not resend this on its own.",
  },
};

export const outcomeFor = (status) => OUTCOME[status] ?? OUTCOME.undecided;

export const CHANNEL_LABELS = {
  whatsapp: "WhatsApp",
  sms: "SMS",
  email: "Email",
  push: "Push",
};

export const channelLabel = (v) => CHANNEL_LABELS[v] ?? humanise(v);

/**
 * `tone_used` always arrives prefixed with "[heuristic]". That prefix means
 * the tone was inferred by Claude from the merchant's industry, not supplied
 * by the merchant. Splitting it out lets the UI label it plainly instead of
 * printing a bracket that looks like a bug.
 */
export function splitTone(toneUsed) {
  if (!toneUsed) return { inferred: false, text: "" };
  const match = /^\s*\[heuristic\]\s*(.*)$/is.exec(toneUsed);
  if (match) return { inferred: true, text: match[1].trim() };
  return { inferred: false, text: String(toneUsed).trim() };
}

/**
 * Root-cause triage prefers a real payment-gateway failure code over the risk
 * event's stated root cause when both exist, so the two can legitimately
 * disagree. Where they do, the detail view says so rather than leaving the
 * reader to assume the agent misfired.
 */
const CAUSE_ACTION_FIT = {
  card_decline: ["retry_payment", "offer_alternate_method", "send_reminder"],
  insufficient_funds: ["send_reminder", "wait_and_retry", "retry_payment"],
  upi_timeout: ["retry_payment", "wait_and_retry"],
  subscription_failure: ["request_mandate_reauth", "send_reminder", "retry_payment"],
  mandate_expired: ["request_mandate_reauth"],
  mandate_revoked: ["request_mandate_reauth", "no_action"],
};

export function isDivergent(rootCause, recommendedAction) {
  if (!rootCause || !recommendedAction) return false;
  const expected = CAUSE_ACTION_FIT[rootCause];
  if (!expected) return false;
  return !expected.includes(recommendedAction);
}
