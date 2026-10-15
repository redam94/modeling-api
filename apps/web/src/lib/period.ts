/** UTC usage periods (a day or a month) as the usage APIs take them (`period`, `start`). */

/** `"2026-10"` / `"2026-10-15"` for the period containing `date` (UTC). */
export function periodKey(period: "day" | "month", date: Date): string {
  const iso = date.toISOString();
  return period === "month" ? iso.slice(0, 7) : iso.slice(0, 10);
}

/** The RFC 3339 instant the API takes as `start` (the first instant of the period). */
export function periodStart(period: "day" | "month", key: string): string {
  return period === "month" ? `${key.slice(0, 7)}-01T00:00:00Z` : `${key.slice(0, 10)}T00:00:00Z`;
}

/** The previous (`-1`) or next (`+1`) period key. */
export function shiftPeriod(period: "day" | "month", key: string, delta: number): string {
  const d = new Date(periodStart(period, key));
  if (period === "month") d.setUTCMonth(d.getUTCMonth() + delta);
  else d.setUTCDate(d.getUTCDate() + delta);
  return periodKey(period, d);
}

export function periodLabel(period: "day" | "month", key: string): string {
  const d = new Date(periodStart(period, key));
  return d.toLocaleDateString("en", period === "month" ? { month: "long", year: "numeric", timeZone: "UTC" } : { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
