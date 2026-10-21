import { describe, expect, it } from "vitest";

import type { DashboardSpec } from "../types";
import {
  channelLabel,
  channelsFrom,
  cleanSpec,
  DEFAULT_SHARE_FORM,
  defaultSpec,
  issueAt,
  nameFromTitle,
  NAME_RE,
  publishPermission,
  refitApprovePermission,
  sameSpec,
  setActuals,
  setRefitWorkflow,
  setWhatIf,
  shareAccessWords,
  shareCreateBody,
  shareFormIssues,
  shareInputsOf,
  shareInputsPermission,
  specIssues,
  tabOfIssue,
  toggleTab,
  toggleTile,
} from "./spec-form";

function spec(patch: Partial<DashboardSpec> = {}): DashboardSpec {
  return {
    ...defaultSpec({
      title: "Weekly media readout",
      source: { kind: "endpoint", alias: "geo-mmm-prod" },
      kpi: { variable: "revenue", label: "Revenue", unit: "currency", currency: "EUR", decimals: 0 },
      channels: ["tv", "paid_search"],
      hasGrid: true,
    }),
    ...patch,
  };
}

const messages = (s: DashboardSpec, grid?: readonly string[] | null) => specIssues(s, { gridChannels: grid }).map((i) => `${i.pointer}: ${i.message}`);

describe("defaults", () => {
  it("names a dashboard after its title", () => {
    expect(nameFromTitle("Weekly media readout")).toBe("weekly-media-readout");
    expect(nameFromTitle("Ventes été 2026!")).toBe("ventes-ete-2026");
    expect(nameFromTitle("2026 plan")).toBe("d-2026-plan");
    expect(nameFromTitle("   ")).toBe("dashboard");
    for (const t of ["Weekly media readout", "2026 plan", "§§§"]) expect(NAME_RE.test(nameFromTitle(t))).toBe(true);
  });

  it("labels channels in plain words", () => {
    expect(channelLabel("tv")).toBe("TV");
    expect(channelLabel("paid_search")).toBe("Paid search");
    expect(channelLabel("spend_social")).toBe("Social");
    expect(channelsFrom(["tv", "tv", "search"], [{ name: "tv", label: "Television", min_mult: 0.2, max_mult: 2 }]).map((c) => c.label)).toEqual(["Television", "Search"]);
  });

  it("starts valid, with what-ifs only on a model with a grid", () => {
    expect(specIssues(spec(), { gridChannels: ["tv", "paid_search"] })).toEqual([]);
    const noGrid = defaultSpec({ title: "Forecast", source: { kind: "endpoint", alias: "sales" }, kpi: { variable: "sales", label: "Sales", unit: "count" }, channels: ["tv"], hasGrid: false });
    expect(noGrid.whatif?.enabled).toBe(false);
    expect(noGrid.tabs).not.toContain("scenarios");
    expect(specIssues(noGrid, { gridChannels: null })).toEqual([]);
  });
});

describe("edits", () => {
  it("keeps at most 4 tiles in a fixed order", () => {
    const four = spec().tiles ?? [];
    expect(toggleTile(four, "accuracy")).toEqual(four);
    expect(toggleTile(toggleTile(four, "expected"), "accuracy")).toEqual(["vs_expected", "return_on_media", "in_range_spend", "accuracy"]);
  });

  it("keeps This period first and always on", () => {
    expect(toggleTab(["this_period", "history"], "trends")).toEqual(["this_period", "trends", "history"]);
    expect(toggleTab(["this_period", "history"], "this_period")).toEqual(["this_period", "history"]);
    expect(toggleTab(["history"], "trends")).toEqual(["this_period", "trends", "history"]);
  });

  it("adds and removes the input tabs with their inputs", () => {
    const off = setWhatIf(spec({ whatif: { enabled: true, exact: true } }), false);
    expect(off.whatif).toEqual({ enabled: false, exact: false });
    expect(off.tabs).not.toContain("scenarios");
    const on = setActuals(spec(), true);
    expect(on.tabs).toContain("update");
    const refit = setActuals({ ...setRefitWorkflow(on, "wf_1"), actuals: { ...on.actuals, refit_workflow_id: "wf_1", allow_refit_requests: true } }, false);
    expect(refit.actuals?.allow_refit_requests).toBe(false);
    expect(setRefitWorkflow({ ...on, actuals: { ...on.actuals, refit_workflow_id: "wf_1", allow_refit_requests: true } }, null).actuals).toMatchObject({ refit_workflow_id: null, allow_refit_requests: false });
  });

  it("saves blank texts as null and compares drafts by content", () => {
    const s = cleanSpec(spec({ client_name: "  ", note: " Net spend. ", kpi: { variable: "orders", label: "Orders", unit: "count", currency: "EUR" } }));
    expect(s.client_name).toBeNull();
    expect(s.note).toBe("Net spend.");
    expect(s.kpi.currency).toBeNull();
    expect(sameSpec(spec({ note: "" }), spec({ note: null }))).toBe(true);
    expect(sameSpec(spec(), spec({ title: "Other" }))).toBe(false);
  });
});

describe("validation messages", () => {
  it("asks for a title, a source and a KPI", () => {
    const bad = spec({ title: " ", source: { kind: "model_version", model_id: null, version: null }, kpi: { variable: "", label: "", unit: "currency", currency: "eu" } });
    expect(messages(bad)).toEqual([
      "/title: Type a title.",
      "/source/model_id: Choose a model.",
      "/kpi/variable: Type the KPI variable of the model (for example revenue).",
      "/kpi/label: Type a label for the KPI.",
      "/kpi/currency: Type a currency code of 3 letters (for example EUR).",
    ]);
    expect(messages(spec({ source: { kind: "model_version", model_id: "mmm", version: null } }))).toEqual(["/source/version: Choose a model version."]);
    expect(messages(spec({ source: { kind: "endpoint", alias: null } }))).toEqual(["/source/alias: Choose an endpoint."]);
  });

  it("checks the lengths of the texts", () => {
    expect(messages(spec({ title: "x".repeat(121), summary: "x".repeat(601), note: "x".repeat(2001), client_name: "x".repeat(81) }))).toEqual([
      "/title: Use 120 characters or fewer for the title.",
      "/client_name: Use 80 characters or fewer for the client name.",
      "/summary: Use 600 characters or fewer for the summary.",
      "/note: Use 2000 characters or fewer for the note.",
    ]);
  });

  it("checks the channels and their slider ranges", () => {
    const s = spec({
      channels: [
        { name: "tv", label: "TV", min_mult: 1, max_mult: 1.5 },
        { name: "tv", label: "TV again" },
        { name: "9x", label: "" },
        { name: "search", label: "Search", min_mult: 0.5, max_mult: 1.5, step: 2 },
      ],
    });
    expect(messages(s)).toEqual([
      "/channels/0/min_mult: The slider range of TV must include the current plan: lowest below 1×, highest above 1× and at most 5×.",
      "/channels/1/name: The channel tv is in the list two times.",
      "/channels/2/name: The channel name “9x” is not correct. Use letters, digits, _, . and -.",
      "/channels/2/label: Type a label for 9x.",
      "/channels/3/step: The slider step of Search must be more than 0 and smaller than its range.",
    ]);
    expect(messages(spec({ channels: [] }))).toEqual(["/channels: Add at least one channel."]);
  });

  it("checks the channels against the response grid", () => {
    expect(messages(spec(), ["tv"])).toEqual(["/channels/1/name: The response grid has no channel named paid_search."]);
    expect(messages(spec(), null)).toEqual(["/whatif/enabled: The model has no response grid. Turn off what-ifs."]);
    expect(messages(setWhatIf(spec(), false), null)).toEqual([]);
  });

  it("checks tiles, tabs and inputs", () => {
    expect(messages(spec({ tiles: ["expected", "vs_expected", "return_on_media", "in_range_spend", "accuracy"] }))).toEqual(["/tiles: Choose at most 4 tiles."]);
    expect(messages(spec({ tabs: ["trends", "this_period"] }))).toEqual(["/tabs: The first tab must be This period."]);
    expect(messages(spec({ whatif: { enabled: false }, tabs: ["this_period", "scenarios"] }))).toEqual(["/tabs: Plan scenarios needs what-ifs. Turn on what-ifs or remove the tab."]);
    expect(messages(spec({ tabs: ["this_period", "update"] }))).toEqual(["/tabs: Update numbers needs uploads. Turn on uploads or remove the tab."]);
    expect(messages(spec({ actuals: { enabled: true, allow_refit_requests: true, refit_workflow_id: null } }))).toEqual(["/actuals/refit_workflow_id: Choose the workflow that refits the model."]);
    expect(messages(spec({ actuals: { enabled: false, allow_refit_requests: true, refit_workflow_id: "wf_1" } }))).toEqual(["/actuals/allow_refit_requests: Refit requests need uploads. Turn on uploads first."]);
    expect(messages(spec({ source: { kind: "model_version", model_id: "m", version: 2 }, refresh: { follow_endpoint: true } }))).toEqual(["/refresh/follow_endpoint: Only a dashboard on an endpoint can follow it."]);
    expect(messages(spec({ limits: { exact_runs_per_day: 501 } }))).toEqual(["/limits/exact_runs_per_day: Use a whole number from 0 to 500."]);
  });

  it("finds a message by pointer and the tab that fixes it", () => {
    const issues = specIssues(spec({ title: "" }));
    expect(issueAt(issues, "/title")).toBe("Type a title.");
    expect(issueAt(specIssues(spec({ channels: [{ name: "tv", label: "" }] })), "/channels", true)).toBe("Type a label for tv.");
    expect(tabOfIssue("/actuals/refit_workflow_id")).toBe("inputs");
    expect(tabOfIssue("/channels/0/name")).toBe("setup");
    expect(tabOfIssue(null)).toBe("setup");
  });
});

describe("share links", () => {
  it("are read-only by default", () => {
    expect(shareCreateBody(DEFAULT_SHARE_FORM, ["whatif", "actuals"])).toEqual({ expires_in_days: 30, allowed_inputs: [] });
    expect(shareAccessWords([])).toBe("Read only");
  });

  it("send only the inputs that the analyst turned on and the dashboard has", () => {
    const f = { ...DEFAULT_SHARE_FORM, label: " Agency ", passcode: "correct horse", whatif: true, actuals: true };
    expect(shareCreateBody(f, ["whatif"])).toEqual({ expires_in_days: 30, allowed_inputs: ["whatif"], label: "Agency", passcode: "correct horse" });
    expect(shareInputsOf({ whatif: { enabled: true }, actuals: { enabled: true } })).toEqual(["whatif", "actuals"]);
    expect(shareInputsOf({ whatif: { enabled: false }, actuals: { enabled: false } })).toEqual([]);
    expect(shareAccessWords(["whatif", "actuals"])).toBe("Read, plus plan scenarios and uploads");
  });

  it("need a passcode of 8 characters or more", () => {
    expect(shareFormIssues({ ...DEFAULT_SHARE_FORM, passcode: "1234567" }).passcode).toBe("Use at least 8 characters for the passcode.");
    expect(shareFormIssues({ ...DEFAULT_SHARE_FORM, passcode: "12345678" })).toEqual({});
    expect(shareFormIssues({ ...DEFAULT_SHARE_FORM, passcode: "x".repeat(129) }).passcode).toBe("Use 128 characters or fewer for the passcode.");
    expect(shareFormIssues({ ...DEFAULT_SHARE_FORM, days: 366 }).days).toBe("A link can last 1 to 365 days.");
  });
});

describe("roles", () => {
  it("publish and refit approval need the runner role, with the reason", () => {
    expect(publishPermission("runner").allowed).toBe(true);
    expect(publishPermission("editor").allowed).toBe(false);
    expect(publishPermission("editor").reason).toContain("You need the runner role");
    expect(publishPermission("viewer").reason).toBe("Only editors and admins can change dashboards.");
    expect(refitApprovePermission("admin").allowed).toBe(true);
    expect(refitApprovePermission("editor").allowed).toBe(false);
    expect(refitApprovePermission("editor").reason).toContain("Approving starts the refit workflow as you");
    expect(refitApprovePermission("client").allowed).toBe(false);
    expect(shareInputsPermission("editor").allowed).toBe(false);
    expect(shareInputsPermission("runner").allowed).toBe(true);
  });
});
