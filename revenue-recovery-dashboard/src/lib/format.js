/** Number, currency and date formatting. */

const inrFull = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

const plain = new Intl.NumberFormat("en-IN");

/** ₹2,44,866.77 — the full Indian-grouped amount, for detail views. */
export function formatINR(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return inrFull.format(value);
}

/**
 * ₹24.49 Cr — lakh/crore compaction, for cards, axes and table columns.
 * Indian financial reporting uses lakh and crore, not K/M/B; a dashboard for
 * this domain that says "244.9M" reads as a foreign import.
 */
export function formatINRCompact(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const n = Number(value);
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(2)} L`;
  if (abs >= 1e3) return `${sign}₹${(abs / 1e3).toFixed(1)}K`;
  return `${sign}₹${abs.toFixed(0)}`;
}

/** 10,166 — grouped count. */
export function formatCount(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return plain.format(value);
}

export function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/**
 * The API returns two different timestamp shapes:
 *   "2026-09-01T05:52:59.301745+00:00"  (offset-aware)
 *   "2024-07-21T04:17:25.000Z"          (Zulu)
 * and SQLite text columns can also yield a bare "2024-07-21 04:17:25" with no
 * zone at all. A bare string is parsed as local time by some engines and UTC
 * by others, which silently shifts every timestamp in the table. Normalise
 * first: treat zoneless values as UTC, which is what the backend writes.
 */
export function parseTimestamp(raw) {
  if (!raw) return null;
  let s = String(raw).trim();
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(s);
  if (!hasZone) s = `${s.replace(" ", "T")}Z`;
  const date = new Date(s);
  return Number.isNaN(date.getTime()) ? null : date;
}

const dateTimeFmt = new Intl.DateTimeFormat("en-IN", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
});

export function formatDateTime(raw) {
  const d = parseTimestamp(raw);
  return d ? dateTimeFmt.format(d) : "—";
}

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS = [
  ["year", 31536000],
  ["month", 2592000],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

/** "3 hours ago". Falls back to an absolute date beyond a year. */
export function formatRelative(raw) {
  const d = parseTimestamp(raw);
  if (!d) return "—";
  const seconds = (d.getTime() - Date.now()) / 1000;
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  for (const [unit, size] of UNITS) {
    if (abs >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return rtf.format(Math.round(seconds / 60), "minute");
}

/** First 8 characters of a UUID — enough to eyeball, short enough to scan. */
export function shortId(id) {
  if (!id) return "—";
  return String(id).slice(0, 8);
}
