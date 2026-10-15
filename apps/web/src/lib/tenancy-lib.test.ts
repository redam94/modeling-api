import { describe, expect, it } from "vitest";

import { atLeast, can, maxRole, minimumRole, type Grants } from "@/api/access";
import { isProjectScoped, parseRetryAfter } from "@/api/client";
import { loginHref, safeReturnTo } from "@/api/session";
import type { Me } from "@/api/types";
import { initials } from "@/components/shell/user-menu";
import { sectionRoot, slugify } from "@/components/shell/project-switcher";
import { buildWorkspace, localMe, projectChoices, resolveActive } from "@/features/auth/workspace";
import { dayBound, parseLimits, tenantChanges } from "@/routes/admin";
import { parseInvitee } from "@/routes/members";
import { validateTokenForm } from "@/routes/tokens";
import { dailySeries, periodKey, periodLabel, periodStart, shiftPeriod } from "@/routes/usage";
import { parseScopeKey, scopeKey } from "@/stores/scope";

import { formatGibSeconds, formatMicros, formatVcpuSeconds, isSessionGroup, runCostView } from "./cost";
import { adviseProblem, humanWait, parseQuotaDetail } from "./problems";

const grants = (patch: Partial<Grants>): Grants => ({ projectRole: null, tenantRole: null, operator: false, ...patch });

describe("permissions (generated from mapi_spec.tenancy.PERMISSIONS)", () => {
  it("orders roles viewer < editor < runner < admin", () => {
    expect(atLeast("runner", "editor")).toBe(true);
    expect(atLeast("editor", "runner")).toBe(false);
    expect(maxRole("viewer", "admin")).toBe("admin");
    expect(maxRole(null, "editor")).toBe("editor");
    expect(minimumRole("run", "model")).toBe("runner");
    expect(minimumRole("update", "dataset")).toBe("editor");
  });

  it("checks project rules against the effective project role", () => {
    expect(can(grants({ projectRole: "runner" }), "create", "run")).toBe(true);
    expect(can(grants({ projectRole: "editor" }), "create", "run")).toBe(false);
    expect(can(grants({ projectRole: "viewer" }), "read", "run")).toBe(true);
    expect(can(grants({ projectRole: "admin" }), "create", "member")).toBe(true);
  });

  it("checks tenant rules against the tenant-wide role only", () => {
    expect(can(grants({ projectRole: "admin", tenantRole: null }), "read", "billing")).toBe(false);
    expect(can(grants({ tenantRole: "admin" }), "read", "billing")).toBe(true);
    expect(can(grants({ tenantRole: "viewer" }), "create", "tenant_member")).toBe(false);
  });

  it("leaves platform actions to operators and denies unknown pairs", () => {
    expect(can(grants({ projectRole: "admin", tenantRole: "admin" }), "update", "kill_switch")).toBe(false);
    expect(can(grants({ operator: true }), "update", "kill_switch")).toBe(true);
    expect(can(grants({ operator: true }), "create", "event")).toBe(false);
    expect(can(grants({ projectRole: "admin" }), "promote", "dataset")).toBe(false);
  });

  it("makes suspended tenants read-only and deleting ones closed", () => {
    expect(can(grants({ projectRole: "admin", tenantStatus: "suspended" }), "read", "run")).toBe(true);
    expect(can(grants({ projectRole: "admin", tenantStatus: "suspended" }), "create", "run")).toBe(false);
    expect(can(grants({ projectRole: "admin", tenantStatus: "deleting" }), "read", "run")).toBe(false);
  });
});

describe("API client helpers", () => {
  it("knows which routes act in the selected project", () => {
    for (const p of ["/v1/runs", "/v1/runs/run_x:cancel", "/v1/datasets/ds_1/versions", "/v1/specs:validate", "/v1/tokens", "/v1/usage", "/v1/plots:validate"]) {
      expect(isProjectScoped(p), p).toBe(true);
    }
    for (const p of ["/v1/me", "/v1/tenants/acme/members", "/v1/admin/audit", "/v1/projects", "/v1/catalog/models", "/v1/schemas/modelspec.json", "/readyz", "/auth/session"]) {
      expect(isProjectScoped(p), p).toBe(false);
    }
  });

  it("reads Retry-After as seconds or an HTTP date", () => {
    const now = Date.parse("2026-10-15T09:30:00Z");
    expect(parseRetryAfter("30", now)).toBe(30);
    expect(parseRetryAfter("Thu, 15 Oct 2026 09:31:00 GMT", now)).toBe(60);
    expect(parseRetryAfter("soon", now)).toBeNull();
    expect(parseRetryAfter(null, now)).toBeNull();
  });

  it("only returns to same-origin paths after sign-in", () => {
    expect(safeReturnTo("/runs?state=failed#x")).toBe("/runs?state=failed#x");
    for (const bad of ["//evil.example/x", "https://evil.example", "/\\evil", "runs", "/a\nb", null]) {
      expect(safeReturnTo(bad)).toBe("/");
    }
  });

  it("builds the BFF login URL with return_to", () => {
    // Tests run in the default (non-mock) mode against the API base.
    expect(loginHref("/tokens")).toBe("http://127.0.0.1:8000/auth/login?return_to=%2Ftokens");
    expect(loginHref("/x", { login_url: "/auth/login?prompt=select_account" })).toBe("http://127.0.0.1:8000/auth/login?prompt=select_account&return_to=%2Fx");
    expect(loginHref("/x", { login_url: "https://evil.example/login" })).toBe("http://127.0.0.1:8000/auth/login?return_to=%2Fx");
    expect(loginHref("//evil")).toBe("http://127.0.0.1:8000/auth/login?return_to=%2F");
  });
});

describe("problem advice", () => {
  it("parses the three quota detail shapes and the kill switches", () => {
    expect(parseQuotaDetail("tenant acme would exceed vcpu_seconds_per_day (3500 + 300 > 3600)")).toEqual({
      level: "tenant",
      subject: "acme",
      limit: "vcpu_seconds_per_day",
      used: 3500,
      requested: 300,
      maximum: 3600,
    });
    expect(parseQuotaDetail("token tok_x limits max_fanout_tasks to 64 (requested 237)")).toMatchObject({ level: "token", limit: "max_fanout_tasks", maximum: 64, requested: 237 });
    expect(parseQuotaDetail("global does not allow max_resource_class XL (maximum L)")).toMatchObject({ level: "global", subject: null, requested: "XL", maximum: "L" });
    expect(parseQuotaDetail("running uploaded plugin code is paused platform-wide")?.limit).toBe("untrusted_execution");
    expect(parseQuotaDetail("the platform's daily spend cap ($500.00) is reached")?.limit).toBe("global_spend");
    expect(parseQuotaDetail("something else")).toBeNull();
  });

  it("explains a daily compute quota with its reset and next steps", () => {
    const a = adviseProblem({ code: "QUOTA_EXCEEDED", title: "Quota exceeded", detail: "tenant acme would exceed vcpu_seconds_per_day (3500 + 300 > 3600)" }, { retryAfter: 7200 });
    expect(a?.kind).toBe("quota");
    expect(a?.title).toBe("Today's compute budget is used up");
    expect(a?.message).toContain("Your organisation (acme)");
    expect(a?.message).toContain("3,500 vCPU·s of 1 vCPU·h is used");
    expect(a?.message).toContain("resets in 2 h");
    expect(a?.actions.map((x) => x.to)).toContain("/usage");
  });

  it("offers a retry after the countdown for rate limits", () => {
    const a = adviseProblem({ code: "RATE_LIMITED", title: "Too many requests", detail: "token tok_x would exceed requests_per_minute (120 + 1 > 120)" }, { retryAfter: 12 });
    expect(a).toMatchObject({ kind: "rate", retryAfter: 12 });
    expect(a?.message).toContain("This API token");
    expect(a?.message).toContain("120 per minute");
    expect(a?.actions[0]?.kind).toBe("retry");
  });

  it("covers the other limits and the kill switches", () => {
    const q = (detail: string) => adviseProblem({ code: "QUOTA_EXCEEDED", title: "Quota exceeded", detail });
    expect(q("tenant acme does not allow max_resource_class L (maximum M)")?.steps[0]).toBe("Choose class M or smaller and try again.");
    expect(q("tenant acme would exceed concurrent_runs (20 + 1 > 20)")?.actions[0]).toMatchObject({ to: "/runs", search: { state: "running" } });
    expect(q("tenant acme would exceed storage_bytes (1073741824 + 10 > 1073741824)")?.message).toContain("1.0 GiB");
    expect(q("running uploaded plugin code is paused platform-wide")?.kind).toBe("paused");
    expect(q("unparseable")?.title).toBe("A quota stops this request");
  });

  it("explains 401 and 403 with the caller's role", () => {
    expect(adviseProblem({ code: "UNAUTHENTICATED", title: "x", detail: null })?.actions[0]?.kind).toBe("signin");
    const f = adviseProblem({ code: "FORBIDDEN", title: "Not allowed", detail: "the viewer role cannot do this" }, { role: "viewer", projectKey: "acme/growth" });
    expect(f?.message).toBe("the viewer role cannot do this. Your role in acme/growth is viewer.");
    expect(adviseProblem({ code: "FORBIDDEN", title: "Not allowed", detail: "the tenant is suspended" })?.kind).toBe("suspended");
    expect(adviseProblem({ code: "NOT_FOUND", title: "Not found", detail: null })).toBeNull();
  });

  it("words waits for people", () => {
    expect(humanWait(42)).toBe("42 s");
    expect(humanWait(600)).toBe("10 min");
    expect(humanWait(7200 + 1200)).toBe("2 h 20 min");
    expect(humanWait(3 * 86400)).toBe("3 days");
  });
});

describe("cost formatting", () => {
  it("formats micros without hiding sub-cent amounts", () => {
    expect(formatMicros(31_200)).toBe("$0.0312");
    expect(formatMicros(1_234_567)).toBe("$1.23");
    expect(formatMicros(0)).toBe("$0.00");
    // a few seconds of compute is a few dozen micros: never rounded away to $0.00
    expect(formatMicros(47)).toBe("$0.000047");
    expect(formatMicros(100)).toBe("$0.0001");
    expect(formatMicros(1)).toBe("$0.000001");
    expect(formatMicros(250_000, "EUR")).toBe("€0.25");
    expect(formatMicros(null)).toBe("—");
  });

  it("formats memory in GiB-seconds or GiB-hours and names session usage", () => {
    expect(formatGibSeconds(52)).toBe("52 GiB·s");
    expect(formatGibSeconds(90_000)).toBe("25 GiB·h");
    expect(formatGibSeconds(null)).toBe("—");
    // the server groups runs started without a token under "-" (mapi_spec.cost)
    expect(isSessionGroup("-")).toBe(true);
    expect(isSessionGroup("tok_01jaf3q2x8m9v7k5h4g3f2e1b2")).toBe(false);
  });

  it("formats compute in seconds or hours", () => {
    expect(formatVcpuSeconds(930)).toBe("930 vCPU·s");
    expect(formatVcpuSeconds(5400)).toBe("1.5 vCPU·h");
  });

  it("shows metered, estimated and v0.2 costs", () => {
    expect(runCostView({ estimated: false, amount_micros: 78_624, price_version: "p1", attempts_billed: 1 })).toMatchObject({ text: "$0.0786", estimated: false });
    expect(runCostView({ estimated: true, amount_micros: 0, estimate_micros: 31_200 })?.text).toBe("≈ $0.0312");
    expect(runCostView({ estimated: true, usd: 0.5 })?.text).toBe("≈ $0.50");
    expect(runCostView({ estimated: true })).toBeNull();
    expect(runCostView(null)).toBeNull();
  });
});

describe("workspace", () => {
  const me: Me = {
    principal: { kind: "user", id: "usr_1", issuer: "https://accounts.google.com", subject: "1", email: "ana@acme.example", name: "Ana" },
    auth: "session",
    operator: true,
    tenants: [
      {
        tenant: { id: "acme", name: "Acme", trust_tier: "verified", created_at: "2026-01-01T00:00:00Z" },
        role: "admin",
        projects: [
          { tenant_id: "acme", project_id: "growth", name: "Growth", role: "viewer" },
          { tenant_id: "acme", project_id: "retail", name: "Retail", role: "admin" },
        ],
      },
      { tenant: { id: "globex", name: "Globex", trust_tier: "public", created_at: "2026-01-01T00:00:00Z" }, role: null, projects: [{ tenant_id: "globex", project_id: "sandbox", name: "Sandbox", role: "runner" }] },
    ],
  };

  it("flattens projects with the effective role", () => {
    const c = projectChoices(me);
    expect(c.map((x) => [x.key, x.role])).toEqual([
      ["acme/growth", "admin"],
      ["acme/retail", "admin"],
      ["globex/sandbox", "runner"],
    ]);
  });

  it("keeps a valid preference and falls back to the first project", () => {
    const c = projectChoices(me);
    expect(resolveActive(c, { tenantId: "globex", projectId: "sandbox" })?.key).toBe("globex/sandbox");
    expect(resolveActive(c, { tenantId: "gone", projectId: "x" })?.key).toBe("acme/growth");
    expect(resolveActive([], null)).toBeNull();
    const ws = buildWorkspace(me, "session", null);
    expect(ws.grants).toMatchObject({ projectRole: "admin", tenantRole: "admin", operator: true });
    expect(ws.user).toEqual({ id: "usr_1", name: "Ana", email: "ana@acme.example" });
  });

  it("models a server without auth as one local project", () => {
    const ws = buildWorkspace(localMe(), "none", null);
    expect(ws.current?.key).toBe("default/default");
    expect(ws.grants.projectRole).toBe("admin");
  });

  it("parses and prints scope keys", () => {
    expect(parseScopeKey("acme/growth")).toEqual({ tenantId: "acme", projectId: "growth" });
    expect(parseScopeKey("acme")).toBeNull();
    expect(parseScopeKey("Acme/x")).toBeNull();
    expect(scopeKey({ tenantId: "a", projectId: "b" })).toBe("a/b");
  });

  it("returns to the section's list after a switch", () => {
    expect(sectionRoot("/runs/run_1")).toBe("/runs");
    expect(sectionRoot("/models/m/versions/2")).toBe("/models");
    expect(sectionRoot("/")).toBe("/");
    expect(slugify("Retail Demand (EU)!")).toBe("retail-demand-eu");
    expect(initials("Ana Analyst")).toBe("AA");
    expect(initials("ana@acme.example")).toBe("AN");
  });
});

describe("forms", () => {
  const base = { name: "ci", scopes: ["models:read"], aliases: "", expires: 90, rate: "", budget: "", period: "month" as const };

  it("validates the token form and builds TokenCreate", () => {
    expect(validateTokenForm(base).body).toEqual({ name: "ci", scopes: ["models:read"], expires_in_days: 90 });
    const full = validateTokenForm({ ...base, aliases: "geo-mmm-prod, *", rate: "120", budget: "36000", period: "day" }).body;
    expect(full).toEqual({
      name: "ci",
      scopes: ["models:read", "score:geo-mmm-prod", "score:*"],
      expires_in_days: 90,
      rate_limit_per_minute: 120,
      vcpu_seconds_budget: 36000,
      budget_period: "day",
    });
    const bad = validateTokenForm({ ...base, name: " ", scopes: [], aliases: "Bad Alias", rate: "1.5", budget: "-1", expires: 999 });
    expect(Object.keys(bad.errors).sort()).toEqual(["aliases", "budget", "expires", "name", "rate"]);
    expect(validateTokenForm({ ...base, scopes: [] }).errors.scopes).toBe("Pick at least one scope.");
  });

  it("tells e-mail invitations from user ids", () => {
    expect(parseInvitee(" Ana@Acme.example ")).toEqual({ email: "ana@acme.example" });
    expect(parseInvitee("usr_01jaf3a9b8c7d6e5f4g3h2j1k0")).toEqual({ principal_id: "usr_01jaf3a9b8c7d6e5f4g3h2j1k0" });
    expect(parseInvitee("not an address")).toHaveProperty("error");
  });

  it("sends only changed tenant fields, with a reason when suspending", () => {
    const t = { id: "acme", name: "Acme", trust_tier: "verified" as const, status: "active" as const, data_residency: "eu", created_at: "x" };
    expect(tenantChanges(t, { name: "Acme", tier: "verified", status: "active", reason: "", residency: "eu" })).toEqual({});
    expect(tenantChanges(t, { name: "Acme 2", tier: "internal", status: "suspended", reason: " overdue ", residency: "us" })).toEqual({
      name: "Acme 2",
      trust_tier: "internal",
      status: "suspended",
      status_reason: "overdue",
      data_residency: "us",
    });
  });

  it("parses quota limits (blank means no limit)", () => {
    const { limits, errors } = parseLimits({ vcpu_seconds_per_day: "72_000", concurrent_runs: "20", max_fanout_tasks: "0", requests_per_minute: "" }, "M");
    expect(limits).toEqual({ vcpu_seconds_per_day: 72000, concurrent_runs: 20, max_resource_class: "M" });
    expect(Object.keys(errors)).toEqual(["max_fanout_tasks"]);
    expect(dayBound("2026-10-15", false)).toBe("2026-10-15T00:00:00Z");
    expect(dayBound("2026-10-15", true)).toBe("2026-10-15T23:59:59Z");
    expect(dayBound("15/10/2026", true)).toBeUndefined();
  });
});

describe("usage periods", () => {
  it("steps through UTC months and days", () => {
    expect(periodKey("month", new Date("2026-10-15T23:00:00Z"))).toBe("2026-10");
    expect(periodStart("month", "2026-10")).toBe("2026-10-01T00:00:00Z");
    expect(shiftPeriod("month", "2026-12", 1)).toBe("2027-01");
    expect(shiftPeriod("day", "2026-03-01", -1)).toBe("2026-02-28");
    expect(periodLabel("month", "2026-10")).toBe("October 2026");
  });

  it("fills days without spend with zeros", () => {
    const series = dailySeries(
      {
        tenant_id: "acme",
        period: "month",
        start: "2026-02-01T00:00:00Z",
        end: "2026-03-01T00:00:00Z",
        totals: {},
        groups: { "2026-02-03": { amount_micros: 500, vcpu_seconds: 10 } },
      },
      "cost",
    );
    expect(series).toHaveLength(28);
    expect(series[2]).toMatchObject({ key: "2026-02-03", label: "3", value: 500 });
    expect(series[0]?.value).toBe(0);
  });
});
