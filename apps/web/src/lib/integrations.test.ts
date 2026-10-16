import { describe, expect, it } from "vitest";

import type { ModelVersion, Problem } from "@/api/types";
import { safeDownloadUrl } from "@/features/trust/data-settings";
import { storeFraction } from "@/features/trust/deletion-progress";
import { publicPathOf, VERIFY_TOKEN_RE } from "@/features/trust/public-paths";
import { signupProblemErrors, validateSignup } from "@/features/trust/public-pages";
import { ApiError } from "@/api/client";
import { modelVersion } from "@/mocks/fixtures";
import { initialTokenForm, validateTokenForm } from "@/routes/tokens";

import { gateFromProblem, previewGate, worstOutcome } from "./gate";
import { iframeSnippet, parseAllowedOrigins, parseCallJson, validateWebhookForm } from "./integrations";

const card = (overall: "pass" | "warn" | "fail", items: NonNullable<ModelVersion["scorecard"]>["items"] = []): Pick<ModelVersion, "scorecard" | "status"> => ({
  status: "ready",
  scorecard: { overall, items },
});

describe("previewGate", () => {
  it("allows a passing scorecard", () => {
    expect(previewGate(card("pass", [{ code: "RHAT_OK", status: "pass", title: "Chains agree", message_plain: "ok" }])).outcome).toBe("allowed");
  });

  it("needs an override for a warning, naming the check", () => {
    const d = previewGate(modelVersion);
    expect(d.outcome).toBe("needs_override");
    expect(d.reasons?.[0]).toMatchObject({ code: "SCORECARD_WARN", outcome: "needs_override", check: "HOLDOUT_MAPE" });
  });

  it("blocks a failing scorecard and an interrupted fit, and asks for an override without a scorecard", () => {
    expect(previewGate(card("fail", [{ code: "X", check: "plausibility.roi", status: "fail", title: "ROI", message_plain: "absurd" }])).outcome).toBe("blocked");
    expect(previewGate({ status: "partial", scorecard: card("pass").scorecard }).outcome).toBe("blocked");
    expect(previewGate({ status: "ready", scorecard: null })).toMatchObject({ outcome: "needs_override", reasons: [{ code: "NO_SCORECARD" }] });
  });

  it("explains a non-passing verdict even without items", () => {
    expect(previewGate(card("warn")).reasons?.[0]?.code).toBe("SCORECARD_WARN");
  });

  it("orders outcomes", () => {
    expect(worstOutcome("allowed", "blocked")).toBe("blocked");
    expect(worstOutcome("needs_override", "allowed")).toBe("needs_override");
  });
});

describe("gateFromProblem", () => {
  const problem = (code: Problem["code"], errors: Problem["errors"] = []): Problem => ({ type: "about:blank", title: "t", status: 409, code, detail: "d", errors });

  it("reads the reasons of OVERRIDE_REQUIRED from the issues' context", () => {
    const d = gateFromProblem(problem("OVERRIDE_REQUIRED", [{ code: "OVERRIDE_REQUIRED", message: "off by 14%", severity: "warning", context: { gate_code: "SCORECARD_WARN", check: "holdout.mape" } }]));
    expect(d).toMatchObject({ outcome: "needs_override", failing_checks: ["holdout.mape"], reasons: [{ code: "SCORECARD_WARN", check: "holdout.mape", message_plain: "off by 14%" }] });
  });

  it("marks blocking reasons and falls back to the detail", () => {
    expect(gateFromProblem(problem("PROMOTION_BLOCKED", [{ code: "PROMOTION_BLOCKED", message: "train only", context: { gate_code: "TRAIN_ONLY" } }]))?.reasons?.[0]).toMatchObject({
      code: "TRAIN_ONLY",
      outcome: "blocked",
    });
    expect(gateFromProblem(problem("PROMOTION_BLOCKED"))?.reasons?.[0]?.message_plain).toBe("d");
  });

  it("ignores unknown gate codes and other problems", () => {
    expect(gateFromProblem(problem("OVERRIDE_REQUIRED", [{ code: "OVERRIDE_REQUIRED", message: "m", context: { gate_code: "<script>" } }]))?.reasons?.[0]?.code).toBe("SCORECARD_WARN");
    expect(gateFromProblem(problem("CONFLICT"))).toBeNull();
    expect(gateFromProblem(null)).toBeNull();
  });
});

describe("webhook and embed forms", () => {
  it("checks the webhook URL's syntax and the events", () => {
    expect(validateWebhookForm({ url: "https://hooks.example.com/mapi", description: "", events: ["run.failed"] })).toEqual({});
    expect(validateWebhookForm({ url: "ftp://x.example.com/a", description: "", events: ["run.failed"] }).url).toMatch(/https/);
    expect(validateWebhookForm({ url: "https://u:p@hooks.example.com/", description: "", events: ["run.failed"] }).url).toMatch(/credentials/);
    expect(validateWebhookForm({ url: "https://hooks.example.com/#x", description: "", events: ["run.failed"] }).url).toMatch(/fragment/);
    expect(validateWebhookForm({ url: "https://hooks.example.com/", description: "", events: [] }).events).toBeDefined();
  });

  it("parses allowed origins like PlotEmbedCreate", () => {
    expect(parseAllowedOrigins("https://acme.notion.site/\nhttps://bi.acme.example:8443, https://acme.notion.site")).toEqual({
      origins: ["https://acme.notion.site", "https://bi.acme.example:8443"],
      error: null,
    });
    expect(parseAllowedOrigins("http://insecure.example").error).toMatch(/https origin/);
    expect(parseAllowedOrigins("https://a.example/path").error).toMatch(/https origin/);
    expect(parseAllowedOrigins(Array.from({ length: 11 }, (_, i) => `https://s${i}.example`).join(" ")).error).toMatch(/10/);
  });

  it("escapes the embed code", () => {
    const html = iframeSnippet('https://plots.example/embed/mpe_x?a=1&b="2"', "<Q4> & co");
    expect(html).toContain('src="https://plots.example/embed/mpe_x?a=1&amp;b=&quot;2&quot;"');
    expect(html).toContain('title="&lt;Q4&gt; &amp; co"');
    expect(html).toContain('referrerpolicy="no-referrer"');
  });
});

describe("the Try it editor", () => {
  it("parses the Try it body", () => {
    expect(parseCallJson('{"mode": "predict"}')).toEqual({ value: { mode: "predict" }, error: null });
    expect(parseCallJson("[1]").error).toMatch(/object/);
    expect(parseCallJson("{").error).toMatch(/Not valid JSON/);
    expect(parseCallJson('{"model": "model://x/y@v1"}').error).toMatch(/model/);
  });
});

describe("tokens for one endpoint", () => {
  it("prefills a score:<alias> token with nothing broader", () => {
    const f = initialTokenForm("score:geo-mmm-prod");
    expect(f).toMatchObject({ name: "geo-mmm-prod-integration", scopes: [], aliases: "geo-mmm-prod", rate: "120" });
    expect(validateTokenForm(f).body).toMatchObject({ scopes: ["score:geo-mmm-prod"], rate_limit_per_minute: 120 });
    expect(initialTokenForm().scopes).toEqual(["models:read"]);
  });

  it("keeps the budget period", () => {
    const f = { ...initialTokenForm(), name: "n", budget: "3600", period: "day" as const };
    expect(validateTokenForm(f).body).toMatchObject({ vcpu_seconds_budget: 3600, budget_period: "day" });
  });
});

describe("trust pages", () => {
  it("validates the sign-up form", () => {
    expect(validateSignup({ email: "ana@acme.example", name: "Ana", organisation: "", terms: true })).toEqual({});
    const e = validateSignup({ email: "ana@localhost", name: " ", organisation: "x".repeat(201), terms: false });
    expect(Object.keys(e).sort()).toEqual(["email", "name", "organisation", "terms"]);
  });

  it("explains a refused address in plain words", () => {
    const disposable = new ApiError(422, { type: "x", title: "t", status: 422, code: "EMAIL_NOT_ALLOWED", detail: "disposable e-mail addresses cannot sign up; use your work address" });
    expect(signupProblemErrors(disposable).email).toMatch(/Disposable e-mail addresses cannot sign up/);
    const other = new ApiError(422, { type: "x", title: "t", status: 422, code: "EMAIL_NOT_ALLOWED", detail: "this e-mail address cannot sign up" });
    expect(signupProblemErrors(other).email).toMatch(/This e-mail address cannot sign up/);
    expect(signupProblemErrors(new ApiError(422, { type: "x", title: "t", status: 422, code: "TERMS_NOT_ACCEPTED" })).terms).toMatch(/terms changed/);
    expect(signupProblemErrors(new Error("x"))).toEqual({});
  });

  it("routes only the public paths outside the gate", () => {
    expect(publicPathOf("/signup")).toBe("/signup");
    expect(publicPathOf("/verify-email/")).toBe("/verify-email");
    expect(publicPathOf("/settings")).toBeNull();
    expect(VERIFY_TOKEN_RE.test(`mev_${"a".repeat(40)}`)).toBe(true);
    expect(VERIFY_TOKEN_RE.test(`mev_${"l".repeat(40)}`)).toBe(false);
  });

  it("measures a store's purge and refuses odd download links", () => {
    expect(storeFraction({ store: "database", state: "running", items_done: 3, items_total: 4 })).toBe(0.75);
    expect(storeFraction({ store: "logs", state: "running" })).toBeNull();
    expect(storeFraction({ store: "queue", state: "done" })).toBe(1);
    expect(safeDownloadUrl("https://storage.example/x.zip?sig=1")).toBe("https://storage.example/x.zip?sig=1");
    expect(safeDownloadUrl("javascript:alert(1)")).toBeNull();
    expect(safeDownloadUrl(null)).toBeNull();
  });
});
