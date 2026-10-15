const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.round((t - now) / 1000);
  const abs = Math.abs(s);
  if (abs < 45) return rtf.format(s, "second");
  if (abs < 45 * 60) return rtf.format(Math.round(s / 60), "minute");
  if (abs < 22 * 3600) return rtf.format(Math.round(s / 3600), "hour");
  if (abs < 26 * 86400) return rtf.format(Math.round(s / 86400), "day");
  return new Date(t).toLocaleDateString("en", { year: "numeric", month: "short", day: "numeric" });
}

export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? "—" : new Date(t).toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function elapsedSeconds(start: string | null | undefined, end?: string | null, now = Date.now()): number | null {
  if (!start) return null;
  const a = Date.parse(start);
  const b = end ? Date.parse(end) : now;
  return Number.isNaN(a) || Number.isNaN(b) ? null : Math.max(0, (b - a) / 1000);
}

export function formatNumber(v: number | null | undefined, digits = 4): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (Number.isInteger(v) && Math.abs(v) < 1e9) return v.toLocaleString("en");
  const abs = Math.abs(v);
  if (abs !== 0 && (abs < 1e-3 || abs >= 1e6)) return v.toExponential(2);
  return Number(v.toPrecision(digits)).toString();
}

export function percent(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${Math.round(v * 100)}%`;
}

/** Shorten long ids/hashes for display while keeping both ends. */
export function shortId(id: string | null | undefined, keep = 6): string {
  if (!id) return "—";
  const sha = /^sha256:([0-9a-f]{64})$/.exec(id);
  if (sha) return `sha256:${sha[1]?.slice(0, 12)}`;
  const us = id.indexOf("_");
  if (us > 0 && id.length > us + 1 + keep * 2) return `${id.slice(0, us + 1)}…${id.slice(-keep)}`;
  return id;
}

/** An ISO timestamp from a data table: the date alone at midnight UTC, else date and time. */
export function formatTimestamp(iso: string): string {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?Z$/.exec(iso);
  if (!m) return iso;
  const [, date, time] = m;
  return time === "00:00" || time === "00:00:00" ? (date ?? iso) : `${date ?? ""} ${time ?? ""}`;
}
