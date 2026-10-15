/**
 * Plain-language advice for the problems people hit on a shared platform: sign-in (401),
 * permissions (403), request rate (429 RATE_LIMITED) and quotas (429 QUOTA_EXCEEDED,
 * including the platform kill switches). The quota detail is the message
 * `mapi_spec.quotas.QuotaDecision` writes ("tenant acme would exceed vcpu_seconds_per_day
 * (3500 + 300 > 3600)"); anything unrecognised falls back to the server's own text.
 */
import type { Problem, ProjectRole } from "@/api/types";

import { formatBytes } from "./format";
import { formatMicros, formatVcpuSeconds } from "./cost";

export type QuotaLevel = "token" | "tenant" | "global";

export interface QuotaFacts {
  level: QuotaLevel;
  /** Tenant id or token id (`null` for the global level). */
  subject: string | null;
  /** A `LimitName` when the server's wording is recognised. */
  limit: string;
  used?: number;
  requested?: number | string;
  maximum?: number | string;
}

const WHO = String.raw`(global|token \S+|tenant \S+)`;
const NUM = String.raw`(-?[\d.]+(?:e[+-]?\d+)?)`;
const EXCEED_RE = new RegExp(String.raw`^${WHO} would exceed (\w+) \(${NUM} \+ ${NUM} > ${NUM}\)$`);
const LIMITS_RE = new RegExp(String.raw`^${WHO} limits (\w+) to ${NUM} \(requested ${NUM}\)$`);
const ALLOW_RE = new RegExp(String.raw`^${WHO} does not allow (\w+) (\S+) \(maximum (\S+)\)$`);

function who(raw: string): Pick<QuotaFacts, "level" | "subject"> {
  if (raw === "global") return { level: "global", subject: null };
  const [level, subject] = raw.split(" ", 2);
  return { level: level === "token" ? "token" : "tenant", subject: subject ?? null };
}

/** The structured facts in a quota problem's `detail`, or `null` when it is free text. */
export function parseQuotaDetail(detail: string | null | undefined): QuotaFacts | null {
  if (!detail) return null;
  const text = detail.trim();
  let m = EXCEED_RE.exec(text);
  if (m) return { ...who(m[1] ?? ""), limit: m[2] ?? "", used: Number(m[3]), requested: Number(m[4]), maximum: Number(m[5]) };
  m = LIMITS_RE.exec(text);
  if (m) return { ...who(m[1] ?? ""), limit: m[2] ?? "", maximum: Number(m[3]), requested: Number(m[4]) };
  m = ALLOW_RE.exec(text);
  if (m) return { ...who(m[1] ?? ""), limit: m[2] ?? "", requested: m[3] ?? "", maximum: m[4] ?? "" };
  if (/uploaded plugin code is paused/i.test(text)) return { level: "global", subject: null, limit: "untrusted_execution" };
  if (/spend cap .* is reached/i.test(text)) return { level: "global", subject: null, limit: "global_spend" };
  return null;
}

export type AdviceKind = "rate" | "quota" | "paused" | "unauthenticated" | "forbidden" | "suspended";

export interface AdviceAction {
  label: string;
  /** `retry`: try again (after the countdown); `signin`: start sign-in; `link`: go to `to`. */
  kind: "retry" | "signin" | "link";
  to?: string;
  search?: Record<string, string>;
}

export interface Advice {
  kind: AdviceKind;
  title: string;
  message: string;
  /** Short, imperative next steps (rendered as a list). */
  steps: string[];
  actions: AdviceAction[];
  retryAfter: number | null;
  facts: QuotaFacts | null;
}

export interface AdviceContext {
  retryAfter?: number | null;
  /** The caller's effective role in the selected project, for 403s. */
  role?: ProjectRole | null;
  /** `tenant/project`, for 403s. */
  projectKey?: string | null;
  tenantSuspended?: boolean;
}

function subjectLabel(f: QuotaFacts): string {
  if (f.level === "global") return "The platform";
  if (f.level === "token") return "This API token";
  return f.subject ? `Your organisation (${f.subject})` : "Your organisation";
}

/** "in 3 h 20 min" / "in 45 s". */
export function humanWait(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  if (h < 48) return rest ? `${h} h ${rest} min` : `${h} h`;
  return `${Math.round(h / 24)} days`;
}

function amount(limit: string, v: number | string | undefined): string {
  if (v === undefined) return "?";
  if (typeof v === "string") return v;
  if (limit.startsWith("vcpu_seconds")) return formatVcpuSeconds(v);
  if (limit === "storage_bytes" || limit === "egress_bytes_per_month") return formatBytes(v);
  if (limit === "global_spend") return formatMicros(v);
  return v.toLocaleString("en");
}

function usage(f: QuotaFacts): string {
  if (f.used !== undefined && f.maximum !== undefined) {
    return ` ${amount(f.limit, f.used)} of ${amount(f.limit, f.maximum)} is used and this needs ${amount(f.limit, f.requested)} more.`;
  }
  if (f.maximum !== undefined) return ` The limit is ${amount(f.limit, f.maximum)}; this asked for ${amount(f.limit, f.requested)}.`;
  return "";
}

const USAGE_LINK: AdviceAction = { label: "See usage and quotas", kind: "link", to: "/usage" };

function quotaAdvice(f: QuotaFacts | null, detail: string | null, retryAfter: number | null): Advice {
  const base = { retryAfter, facts: f } as const;
  const when = retryAfter != null ? ` It resets in ${humanWait(retryAfter)}.` : "";
  const who = f ? subjectLabel(f) : "A quota";
  const limit = f?.limit ?? "";
  switch (limit) {
    case "max_resource_class":
      return {
        ...base,
        kind: "quota",
        title: f?.requested !== undefined ? `Resource class ${String(f.requested)} is not available` : "This resource class is not available",
        message: `${who} can use resource classes up to ${String(f?.maximum ?? "?")}.`,
        steps: [`Choose class ${String(f?.maximum ?? "S")} or smaller and try again.`, "Ask a platform operator if your work needs a larger class."],
        actions: [],
      };
    case "max_fanout_tasks":
      return {
        ...base,
        kind: "quota",
        title: "Too many parallel tasks",
        message: `${who} limits one run to ${amount(limit, f?.maximum)} tasks; this run asked for ${amount(limit, f?.requested)}.`,
        steps: ["Split the sweep into smaller parts, or reduce the number of simulations."],
        actions: [],
      };
    case "concurrent_runs":
      return {
        ...base,
        kind: "quota",
        title: "Too many runs at once",
        message: `${who} can have ${amount(limit, f?.maximum)} runs going at the same time, and all of them are in use.`,
        steps: ["Wait for a run to finish, or cancel one you no longer need, then try again."],
        actions: [{ label: "Show running runs", kind: "link", to: "/runs", search: { state: "running" } }, { label: "Try again", kind: "retry" }],
      };
    case "vcpu_seconds_per_day":
    case "vcpu_seconds_per_month": {
      const period = limit.endsWith("day") ? "today's" : "this month's";
      return {
        ...base,
        kind: "quota",
        title: `${period[0]?.toUpperCase()}${period.slice(1)} compute budget is used up`,
        message: `${who} has a compute budget.${f ? usage(f) : ""}${when}`,
        steps: [
          "Use a smaller resource class, fewer draws or chains, or a shorter horizon.",
          limit.endsWith("day") ? "Try again after midnight UTC." : "Try again next month.",
          "Ask a platform operator to raise the quota if you need more.",
        ],
        actions: [USAGE_LINK],
      };
    }
    case "storage_bytes":
      return {
        ...base,
        kind: "quota",
        title: "Storage is full",
        message: `${who} has used its storage.${f ? usage(f) : ""} Reading and deleting still work.`,
        steps: ["Delete dataset versions or model versions you no longer need, then try again."],
        actions: [USAGE_LINK, { label: "Open datasets", kind: "link", to: "/datasets" }],
      };
    case "egress_bytes_per_month":
      return {
        ...base,
        kind: "quota",
        title: "Download allowance used up",
        message: `${who} has used this month's download allowance.${f ? usage(f) : ""}${when}`,
        steps: ["Download fewer or smaller artifacts, or wait for the next month."],
        actions: [USAGE_LINK],
      };
    case "untrusted_execution":
      return {
        ...base,
        kind: "paused",
        title: "Uploaded code is paused",
        message: "Platform operators have paused running uploaded plugin code. Built-in models and steps still run.",
        steps: ["Use a built-in model for now, or try again later."],
        actions: [],
      };
    case "global_spend":
      return {
        ...base,
        kind: "paused",
        title: "The platform is at its spend cap",
        message: `New runs are paused until the platform's spend window resets.${when}`,
        steps: ["Try again later. Runs already in progress are not affected."],
        actions: [{ label: "Try again", kind: "retry" }],
      };
    default:
      return {
        ...base,
        kind: "quota",
        title: "A quota stops this request",
        message: detail ?? "A usage limit was reached.",
        steps: ["Check usage and quotas, or ask a platform operator."],
        actions: [USAGE_LINK],
      };
  }
}

/**
 * Advice for a problem the user can act on, or `null` for ordinary errors (validation, not
 * found, run failures), which keep their technical presentation.
 */
export function adviseProblem(problem: Pick<Problem, "code" | "detail" | "title">, ctx: AdviceContext = {}): Advice | null {
  const retryAfter = ctx.retryAfter ?? null;
  switch (problem.code) {
    case "RATE_LIMITED": {
      const f = parseQuotaDetail(problem.detail);
      const wait = retryAfter ?? 60;
      return {
        kind: "rate",
        title: "Too many requests",
        message: `${f ? subjectLabel(f) : "You"} sent more requests in the last minute than allowed${
          f?.maximum !== undefined ? ` (${amount("requests_per_minute", f.maximum)} per minute)` : ""
        }. Nothing was changed.`,
        steps: [`Wait ${humanWait(wait)}, then try again.`],
        actions: [{ label: "Try again", kind: "retry" }],
        retryAfter: wait,
        facts: f,
      };
    }
    case "QUOTA_EXCEEDED":
      return quotaAdvice(parseQuotaDetail(problem.detail), problem.detail ?? null, retryAfter);
    case "UNAUTHENTICATED":
      return {
        kind: "unauthenticated",
        title: "Please sign in again",
        message: "Your session has ended. Sign in to continue; what you see on this page stays as it is.",
        steps: [],
        actions: [{ label: "Sign in", kind: "signin" }],
        retryAfter: null,
        facts: null,
      };
    case "FORBIDDEN": {
      if (ctx.tenantSuspended || /suspend/i.test(problem.detail ?? "")) {
        return {
          kind: "suspended",
          title: "This organisation is read-only",
          message: "The organisation is suspended, so nothing can be changed or run. You can still look at everything.",
          steps: ["Contact your organisation's admin or the platform operators."],
          actions: [],
          retryAfter: null,
          facts: null,
        };
      }
      const role = ctx.role ? ` Your role${ctx.projectKey ? ` in ${ctx.projectKey}` : ""} is ${ctx.role}.` : "";
      return {
        kind: "forbidden",
        title: "You don't have permission to do this",
        message: `${problem.detail ? `${problem.detail.replace(/\.?$/, ".")}` : "Your role does not allow this action."}${role}`,
        steps: ["Ask a project admin for a higher role (runner to start runs, editor to change data and specs)."],
        actions: [{ label: "See project members", kind: "link", to: "/members" }],
        retryAfter: null,
        facts: null,
      };
    }
    default:
      return null;
  }
}
