/**
 * Number and date text for client-facing readouts (docs/project/writing-style.md): the U+2212
 * minus, DD/MM/YYYY dates, and an em dash for a missing value. Pure functions, no locale state,
 * so the share app and the readout charts render the same text everywhere.
 */

export const MINUS = "−";
export const MISSING = "—";

/** Swap the ASCII hyphen-minus of a formatted number for U+2212. */
export function minus(s: string): string {
  return s.replace(/^-/, MINUS).replace(/(\s|\()-(?=\d)/g, `$1${MINUS}`);
}

/** A fixed number of decimals; −0.00 reads 0.00. */
export function fixed(v: number | null | undefined, decimals = 2): string {
  if (v == null || !Number.isFinite(v)) return MISSING;
  const s = v.toFixed(decimals);
  return Number(s) === 0 ? (0).toFixed(decimals) : minus(s);
}

/** Like {@link fixed} with an explicit sign: "+0.04", "−0.12", "0.00". */
export function signed(v: number | null | undefined, decimals = 2): string {
  if (v == null || !Number.isFinite(v)) return MISSING;
  const s = fixed(v, decimals);
  return Number(v.toFixed(decimals)) > 0 ? `+${s}` : s;
}

/** Thousands separators and a fixed number of decimals ("12,400", "−3.5"). */
export function grouped(v: number | null | undefined, decimals = 0): string {
  if (v == null || !Number.isFinite(v)) return MISSING;
  const s = v.toLocaleString("en-GB", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return /^-?0(\.0+)?$/.test(s) ? s.replace(/^-/, "") : minus(s);
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})/;

/** An ISO date (or timestamp) as DD/MM/YYYY; any other text is returned unchanged. */
export function dmy(t: string): string {
  const m = ISO_DATE.exec(t);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : t;
}

/** The short axis form of an ISO date: DD/MM. */
export function dm(t: string): string {
  const m = ISO_DATE.exec(t);
  return m ? `${m[3]}/${m[2]}` : t;
}

/**
 * The axis label of each date of `ts`: DD/MM while those are all different; else, when every
 * date is the first of its month (months, quarters, years), MM/YYYY; else DD/MM/YY. So a
 * quarterly axis over several years never shows "01/10" four times.
 */
export function axisDate(ts: readonly string[]): (t: string) => string {
  const dates = ts.map((t) => ISO_DATE.exec(t)).filter((m): m is RegExpExecArray => m !== null);
  const short = new Set(dates.map((m) => `${m[3]}/${m[2]}`));
  if (short.size === dates.length) return dm;
  if (dates.every((m) => m[3] === "01")) {
    const monthly = new Set(dates.map((m) => `${m[2]}/${m[1]}`));
    if (monthly.size === dates.length) return (t) => { const m = ISO_DATE.exec(t); return m ? `${m[2]}/${m[1]}` : t; };
  }
  return (t) => { const m = ISO_DATE.exec(t); return m ? `${m[3]}/${m[2]}/${(m[1] ?? "").slice(2)}` : t; };
}
