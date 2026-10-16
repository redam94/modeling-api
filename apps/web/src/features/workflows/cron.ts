/**
 * A port of `mapi_spec.cron` for the schedule editor's preview: parse a five-field cron
 * expression (or a macro), list the next firing times in an IANA time zone (a local time
 * skipped by a daylight-saving jump does not fire; a repeated one fires once, at its first
 * occurrence), and refuse schedules that fire more often than every 15 minutes. The server
 * decides (`SCHEDULE_INVALID`); the preview only shows what it will do.
 */

export const MIN_INTERVAL_MINUTES = 15;

const MACROS: Record<string, string> = {
  "@hourly": "0 * * * *",
  "@daily": "0 0 * * *",
  "@midnight": "0 0 * * *",
  "@weekly": "0 0 * * 0",
  "@monthly": "0 0 1 * *",
  "@yearly": "0 0 1 1 *",
  "@annually": "0 0 1 1 *",
};
const MONTHS = Object.fromEntries(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].map((m, i) => [m, i + 1]));
const DAYS = Object.fromEntries(["sun", "mon", "tue", "wed", "thu", "fri", "sat"].map((d, i) => [d, i]));
const FIELD_RE = /^[0-9a-z*/,-]{1,100}$/;

export class CronError extends Error {
  override name = "CronError";
}

export interface CronSpec {
  minutes: Set<number>;
  hours: Set<number>;
  days: Set<number>;
  months: Set<number>;
  /** 0 = Sunday … 6 = Saturday. */
  weekdays: Set<number>;
  daysAny: boolean;
  weekdaysAny: boolean;
}

function value(token: string, lo: number, hi: number, names?: Record<string, number>): number {
  if (names && token in names) return names[token]!;
  if (!/^\d{1,2}$/.test(token)) throw new CronError(`"${token}" is not a number in ${lo}-${hi}`);
  const v = Number(token);
  if (v < lo || v > hi) throw new CronError(`${v} is outside ${lo}-${hi}`);
  return v;
}

function field(text: string, lo: number, hi: number, names?: Record<string, number>): Set<number> {
  if (!FIELD_RE.test(text)) throw new CronError(`invalid cron field "${text.slice(0, 40)}"`);
  const out = new Set<number>();
  for (const part of text.split(",")) {
    if (!part) throw new CronError("empty list item");
    const [rng = "", stepText] = part.split("/", 2) as [string, string | undefined];
    let step = 1;
    if (stepText !== undefined) {
      if (!/^\d+$/.test(stepText) || Number(stepText) < 1 || Number(stepText) > hi - lo + 1) throw new CronError(`invalid step in "${part}"`);
      step = Number(stepText);
    }
    let a: number;
    let b: number;
    if (rng === "*") [a, b] = [lo, hi];
    else if (rng.includes("-")) {
      const [left = "", right = ""] = rng.split("-", 2);
      a = value(left, lo, hi, names);
      b = value(right, lo, hi, names);
      if (a > b) throw new CronError(`range "${part}" runs backwards`);
    } else {
      a = value(rng, lo, hi, names);
      b = stepText !== undefined ? hi : a;
    }
    for (let v = a; v <= b; v += step) out.add(v);
  }
  return out;
}

export function parseCron(expr: string): CronSpec {
  if (typeof expr !== "string" || expr.length > 200) throw new CronError("a cron expression is a short text");
  const lower = expr.trim().toLowerCase();
  const text = MACROS[lower] ?? lower;
  const fields = text.split(/\s+/).filter(Boolean);
  if (fields.length !== 5) throw new CronError("a cron expression has 5 fields: minute hour day month weekday");
  const [minute, hour, dom, month, dow] = fields as [string, string, string, string, string];
  const weekdays = new Set([...field(dow, 0, 7, DAYS)].map((d) => (d === 7 ? 0 : d)));
  return {
    minutes: field(minute, 0, 59),
    hours: field(hour, 0, 23),
    days: field(dom, 1, 31),
    months: field(month, 1, 12, MONTHS),
    weekdays,
    daysAny: dom === "*",
    weekdaysAny: dow === "*",
  };
}

function matchesDay(cs: CronSpec, day: number, weekday: number): boolean {
  const dom = cs.days.has(day);
  const dow = cs.weekdays.has(weekday);
  if (cs.daysAny && cs.weekdaysAny) return true;
  if (cs.daysAny) return dow;
  if (cs.weekdaysAny) return dom;
  return dom || dow;
}

interface Wall {
  y: number;
  mo: number;
  d: number;
  h: number;
  mi: number;
}

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(tz: string): Intl.DateTimeFormat {
  let f = formatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
    });
    formatters.set(tz, f);
  }
  return f;
}

/** Is `tz` an IANA zone this browser knows? */
export function isTimeZone(tz: string): boolean {
  if (!/^[A-Za-z0-9_+-]+(?:\/[A-Za-z0-9_+-]+){0,2}$/.test(tz)) return false;
  try {
    formatter(tz);
    return true;
  } catch {
    return false;
  }
}

/** The wall-clock time of an instant in `tz`. */
function wallOf(ms: number, tz: string): Wall {
  const parts = Object.fromEntries(formatter(tz).formatToParts(new Date(ms)).map((p) => [p.type, p.value]));
  return { y: Number(parts.year), mo: Number(parts.month), d: Number(parts.day), h: Number(parts.hour), mi: Number(parts.minute) };
}

const wallMs = (w: Wall) => Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi);
const sameWall = (a: Wall, b: Wall) => a.y === b.y && a.mo === b.mo && a.d === b.d && a.h === b.h && a.mi === b.mi;

/** The first instant whose wall clock in `tz` reads `w`, or `null` in a daylight-saving gap. */
function instantOf(w: Wall, tz: string): number | null {
  const naive = wallMs(w);
  const found: number[] = [];
  for (const probe of [naive - 14 * 3600_000, naive + 14 * 3600_000, naive]) {
    const offset = wallMs(wallOf(probe, tz)) - probe;
    const t = naive - offset;
    if (sameWall(wallOf(t, tz), w)) found.push(t);
  }
  return found.length ? Math.min(...found) : null;
}

/** The first firing time strictly after `after` (ms), as epoch ms. */
export function nextFire(spec: CronSpec | string, after: number, tz = "UTC"): number {
  const cs = typeof spec === "string" ? parseCron(spec) : spec;
  if (!isTimeZone(tz)) throw new CronError(`unknown time zone "${tz.slice(0, 64)}"`);
  // The local minute after `after`, seconds dropped.
  const start = wallOf(after, tz);
  const startMs = wallMs(start) + 60_000;
  const hours = [...cs.hours].sort((a, b) => a - b);
  const minutes = [...cs.minutes].sort((a, b) => a - b);
  const day0 = Date.UTC(start.y, start.mo - 1, start.d);
  for (let i = 0; i < 366 * 5 + 2; i++) {
    const day = new Date(day0 + i * 86_400_000);
    const mo = day.getUTCMonth() + 1;
    if (!cs.months.has(mo) || !matchesDay(cs, day.getUTCDate(), day.getUTCDay())) continue;
    for (const h of hours) {
      for (const mi of minutes) {
        const w: Wall = { y: day.getUTCFullYear(), mo, d: day.getUTCDate(), h, mi };
        if (wallMs(w) < startMs) continue;
        const t = instantOf(w, tz);
        if (t !== null) return t;
      }
    }
  }
  throw new CronError("the schedule never fires");
}

/** The next `n` firing times after `after`. */
export function nextFires(expr: string, tz: string, n = 5, after = Date.now()): number[] {
  const cs = parseCron(expr);
  const out: number[] = [];
  let t = after;
  for (let i = 0; i < n; i++) {
    t = nextFire(cs, t, tz);
    out.push(t);
  }
  return out;
}

/** `mapi_spec.cron.check_cron`: a problem in plain words, or `null` when the server will accept it. */
export function cronProblem(expr: string, minInterval = MIN_INTERVAL_MINUTES): string | null {
  let cs: CronSpec;
  try {
    cs = parseCron(expr);
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
  const t = Date.UTC(2026, 0, 5); // a Monday
  try {
    let prev = nextFire(cs, t);
    for (let i = 0; i < 600; i++) {
      const next = nextFire(cs, prev);
      if (next - prev < minInterval * 60_000) return `schedules may fire at most every ${minInterval} minutes`;
      if (next - t > 8 * 86_400_000) break;
      prev = next;
    }
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
  return null;
}

/** A plain-language reading of common expressions (falls back to the expression itself). */
export function describeCron(expr: string): string {
  const lower = expr.trim().toLowerCase();
  const named: Record<string, string> = {
    "@hourly": "Every hour, on the hour",
    "@daily": "Every day at midnight",
    "@midnight": "Every day at midnight",
    "@weekly": "Every Sunday at midnight",
    "@monthly": "On the 1st of every month at midnight",
    "@yearly": "Every 1 January at midnight",
    "@annually": "Every 1 January at midnight",
  };
  if (named[lower]) return named[lower];
  const f = lower.split(/\s+/);
  if (f.length !== 5) return expr;
  const [mi, h, dom, mo, dow] = f as [string, string, string, string, string];
  const at = /^\d+$/.test(mi) && /^\d+$/.test(h) ? `at ${h.padStart(2, "0")}:${mi.padStart(2, "0")}` : null;
  const dayNames = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  if (at && dom === "*" && mo === "*" && dow === "*") return `Every day ${at}`;
  if (at && dom === "*" && mo === "*" && /^[0-7]$/.test(dow)) return `Every ${dayNames[Number(dow) % 7]} ${at}`;
  if (at && dom === "*" && mo === "*" && dow === "1-5") return `Every weekday ${at}`;
  if (at && /^\d+$/.test(dom) && mo === "*" && dow === "*") return `On day ${dom} of every month ${at}`;
  const every = /^\*\/(\d+)$/.exec(mi);
  if (every && h === "*" && dom === "*" && mo === "*" && dow === "*") return `Every ${every[1]} minutes`;
  return expr;
}

export const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Every Monday at 06:00", cron: "0 6 * * 1" },
  { label: "Every day at 06:00", cron: "0 6 * * *" },
  { label: "Every weekday at 07:00", cron: "0 7 * * 1-5" },
  { label: "On the 1st of every month", cron: "0 6 1 * *" },
  { label: "Every hour", cron: "@hourly" },
];
