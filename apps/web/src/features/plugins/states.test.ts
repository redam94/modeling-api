import { File as NodeFile } from "node:buffer";

import { setupServer } from "msw/node";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import type { ProjectScope } from "@/api/queries";
import type { CatalogResponse, Me } from "@/api/types";
import { env } from "@/env";
import { isPluginRunKind, PLUGIN_RUN_KINDS, RUN_KINDS } from "@/lib/run-state";
import { createHandlers, createState } from "@/mocks/handlers";
import { ACME_NS, ANA_ID, BEA_ID, MOCK_DEV_UNTRUSTED_KEY, MOCK_PLUGIN_MS_KEY, MOCK_PLUGIN_REAUTH_KEY, MOCK_PLUGINS_OFF_KEY, mockPluginFlags, PLUGIN_ACME, PLUGIN_GEO, V } from "@/mocks/plugins";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";

import { isReauthRequired, isStateConflict, pluginFlags, uploadWheel, versionPollDelay } from "./api";
import { pk } from "./keys";
import {
  canRequestApproval,
  canTransition,
  canYank,
  decisionBlockReason,
  decisionBody,
  exportsSummary,
  groupKit,
  hasUnsafeText,
  INSPECT_CHECK_LABEL,
  isApproved,
  isAwaitingApproval,
  isChecking,
  isDecidable,
  isFailed,
  isKitFailed,
  isKitPassed,
  isMountable,
  isPluginVersionState,
  isPrivate,
  isPurged,
  isRefused,
  isRejected,
  isState,
  isUploadedId,
  KIT_FAILED_ISSUES_DESCRIPTION,
  stateDescription,
  isUploadedNamespace,
  isYanked,
  kitCounts,
  parseUploadNamespace,
  parseWheelName,
  PLUGIN_TRANSITIONS,
  referenceLink,
  sbomSummary,
  STATE_DESCRIPTION,
  STATE_LABEL,
  STATE_TONE,
  uploadDistName,
  uploadedCatalogEntries,
  uploadedNamespaceOf,
  uploadVersionIdOf,
  wheelFileProblem,
} from "./states";
import { PLUGIN_VERSION_STATES, type Plugin, type PluginSettings, type PluginVersion, type PluginVersionState, type PluginVersionSummary, type VerifiedUploader } from "./types";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => {
  localStorage.clear();
  server.resetHandlers(...createHandlers(createState()));
});

type Who = keyof typeof PERSONAS;

async function call<T>(who: Who, method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<{ status: number; data: T }> {
  localStorage.setItem(MOCK_SESSION_KEY, who);
  const res = await fetch(`${env.apiBase}${path}`, {
    method,
    headers: { "X-Mapi-Project": "acme/growth", "X-CSRF-Token": mockCsrfToken(PERSONAS[who]!), ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, data: (text ? JSON.parse(text) : null) as T };
}

const version = async (id: string, who: Who = "ana", plugin = PLUGIN_ACME) => (await call<PluginVersion>(who, "GET", `/v1/plugins/${plugin}/versions/${id}`)).data;
const versions = async (who: Who = "ana", plugin = PLUGIN_ACME) => (await call<{ items: PluginVersionSummary[] }>(who, "GET", `/v1/plugins/${plugin}/versions`)).data.items;

function meOf(who: Who, auth: Me["auth"] = "session"): Me {
  const p = PERSONAS[who]!;
  return {
    auth,
    principal: { kind: "user", id: p.id, issuer: "https://accounts.google.com", subject: p.id.slice(4), email: p.email, name: p.name },
    tenants: Object.entries(p.tenants).map(([id, role]) => ({ tenant: { id, name: id, trust_tier: "verified", status: "active", created_at: "2026-09-01T08:00:00Z" }, role, projects: [] })),
  };
}

function scope(who: Who): ProjectScope {
  return { key: "acme/growth", tenantId: "acme", projectId: "growth", headers: { "X-Mapi-Project": "acme/growth", "X-CSRF-Token": mockCsrfToken(PERSONAS[who]!) } };
}

/**
 * A wheel the test's `fetch` can send: jsdom's File and FormData do not stream through Node's
 * fetch, so the upload tests use Node's own classes (the browser has one set of both).
 */
const wheel = (name: string, size = 2048) => new NodeFile([new Uint8Array(size)], name, { type: "application/zip" }) as unknown as File;
let jsdomFormData: typeof FormData;
beforeAll(async () => {
  jsdomFormData = globalThis.FormData;
  const probe = await new Response("a=1", { headers: { "content-type": "application/x-www-form-urlencoded" } }).formData();
  globalThis.FormData = probe.constructor as typeof FormData;
});
afterAll(() => {
  globalThis.FormData = jsdomFormData;
});

// ------------------------------------------------------------------------------ states

describe("plugin version states", () => {
  it("label, colour and describe every state", () => {
    for (const s of PLUGIN_VERSION_STATES) {
      expect(STATE_LABEL[s]).toBeTruthy();
      expect(STATE_TONE[s]).toBeTruthy();
      expect(STATE_DESCRIPTION[s]).toBeTruthy();
      expect(isPluginVersionState(s)).toBe(true);
    }
    expect(isPluginVersionState("shipped")).toBe(false);
    expect(STATE_LABEL.inspect_failed).toBe("Refused");
    expect(STATE_LABEL.kit_passed).toBe("Ready for you");
    expect(STATE_LABEL.approved).toBe("Approved for the team");
    expect(STATE_TONE.inspect_failed).toBe("danger");
  });

  it("mirror mapi_spec.plugins.PLUGIN_TRANSITIONS exactly", () => {
    const edges = PLUGIN_VERSION_STATES.flatMap((from) => PLUGIN_TRANSITIONS[from].map((to) => `${from}>${to}`)).sort();
    expect(edges).toEqual(
      [
        "uploaded>inspecting",
        "inspecting>inspect_failed",
        "inspecting>describing",
        "inspect_failed>purged",
        "describing>kit_failed",
        "describing>kit_passed",
        "kit_failed>purged",
        "kit_passed>awaiting_approval",
        "kit_passed>yanked",
        "awaiting_approval>approved",
        "awaiting_approval>rejected",
        "awaiting_approval>yanked",
        "approved>yanked",
        "approved>awaiting_approval",
        "rejected>purged",
        "yanked>purged",
      ].sort(),
    );
    expect(canTransition("approved", "kit_passed")).toBe(false);
    expect(canTransition("purged", "uploaded")).toBe(false);
  });

  it("have one type guard per state, true only for that state", () => {
    const guards: Record<PluginVersionState, (v: PluginVersionSummary) => boolean> = {
      uploaded: (v) => isState(v, "uploaded"),
      inspecting: (v) => isState(v, "inspecting"),
      inspect_failed: isRefused,
      describing: (v) => isState(v, "describing"),
      kit_failed: isKitFailed,
      kit_passed: isKitPassed,
      awaiting_approval: isAwaitingApproval,
      approved: isApproved,
      rejected: isRejected,
      yanked: isYanked,
      purged: isPurged,
    };
    const base = { id: "plv_x", plugin_id: "plg_x", version: "1", scope: "none", uploaded_by: "u", uploaded_at: "2026-10-22T00:00:00Z" } as const;
    for (const s of PLUGIN_VERSION_STATES) {
      const v: PluginVersionSummary = { ...base, state: s };
      for (const t of PLUGIN_VERSION_STATES) expect(guards[t](v), `${t} on ${s}`).toBe(s === t);
      expect(isMountable(v)).toBe(["kit_passed", "awaiting_approval", "approved"].includes(s));
      expect(isChecking(v)).toBe(["uploaded", "inspecting", "describing"].includes(s));
      expect(isFailed(v)).toBe(["inspect_failed", "kit_failed", "rejected"].includes(s));
      expect(versionPollDelay(v)).toBe(isChecking(v) ? 2000 : false);
    }
  });
});

// ------------------------------------------------------------------------------ ids and text

describe("uploaded namespaces and ids", () => {
  it("parse t_<code>_<short> and refuse ambiguous forms", () => {
    expect(parseUploadNamespace("t_k3vq7m_acme")).toEqual({ code: "k3vq7m", short: "acme" });
    expect(parseUploadNamespace("t_abcdef_ab")).toEqual({ code: "abcdef", short: "ab" });
    expect(parseUploadNamespace("t_abcde_fab")).toBeNull(); // code is 6 characters
    expect(parseUploadNamespace("t_k3vq7m_a")).toBeNull(); // short name has 2+ characters
    expect(parseUploadNamespace("t_k3vq1m_acme")).toBeNull(); // 1 is not base32
    expect(parseUploadNamespace("t_k3vq7m_acme_x")).toBeNull();
    expect(isUploadedNamespace("core")).toBe(false);
    expect(uploadDistName("t_k3vq7m_acme")).toBe("t-k3vq7m-acme");
  });

  it("know all five id forms and never call a built-in uploaded", () => {
    expect(uploadedNamespaceOf("t_k3vq7m_acme.store_mean")).toBe("t_k3vq7m_acme"); // model
    expect(uploadedNamespaceOf("t_k3vq7m_acme.winsorize@^1")).toBe("t_k3vq7m_acme"); // step ref
    expect(uploadedNamespaceOf("transform.t_k3vq7m_acme_clean")).toBe("t_k3vq7m_acme"); // node
    expect(uploadedNamespaceOf("t_k3vq7m_acme_weekly_fit")).toBe("t_k3vq7m_acme"); // template
    for (const id of ["core.adstock", "mmm.bayesian", "transform.clean", "geo_lift_weekly", "t_k3vq7m_acme.", "acme.winsorize"]) expect(isUploadedId(id), id).toBe(false);
    expect(uploadVersionIdOf(`upload:${V.approved}`)).toBe(V.approved);
    expect(uploadVersionIdOf("acme-mapi-extras")).toBeNull();
    expect(uploadVersionIdOf("upload:")).toBeNull();
  });

  it("allow only safe https links from an uploaded About card", () => {
    expect(referenceLink("https://example.org/acme")).toEqual({ ok: true, href: "https://example.org/acme", host: "example.org" });
    expect(referenceLink("https://xn--bcher-kva.example/docs")).toMatchObject({ ok: true, host: "xn--bcher-kva.example" });
    for (const bad of ["javascript:alert(1)", "http://example.org", "https://user@evil.example/", "https://bücher.example/", "https://10.0.0.1/", "https://[::1]/", "https://exa‮mple.org/", "https://example .org", "https://localhost/", "data:text/html,x"]) {
      expect(referenceLink(bad).ok, bad).toBe(false);
    }
  });

  it("spot hidden characters", () => {
    expect(hasUnsafeText("Store chart")).toBe(false);
    expect(hasUnsafeText("line\nbreak\ttab")).toBe(false);
    expect(hasUnsafeText("Store ‮evil")).toBe(true);
    expect(hasUnsafeText("zero​width")).toBe(true);
    expect(hasUnsafeText("é")).toBe(true); // not NFC
  });

  it("check a wheel file before asking for a ticket", () => {
    expect(wheelFileProblem("t_k3vq7m_acme-0.4.0-py3-none-any.whl", 2048, 20_971_520)).toBeNull();
    expect(wheelFileProblem("acme-0.4.0.tar.gz", 2048, 20_971_520)).toMatch(/Upload a wheel/);
    expect(wheelFileProblem("acme-0.4.0-cp312-cp312-linux_x86_64.whl", 2048, 20_971_520)).toMatch(/pure-Python/);
    expect(wheelFileProblem("t_k3vq7m_acme-0.4.0-py3-none-any.whl", 21_000_000, 20_971_520)).toMatch(/20 MiB/);
    expect(wheelFileProblem("bad name-0.1-py3-none-any.whl", 10, 20_971_520)).toMatch(/letters/);
    expect(parseWheelName("t_k3vq7m_acme-0.4.0-py3-none-any.whl")).toEqual({ dist: "t_k3vq7m_acme", version: "0.4.0" });
    expect(Object.keys(INSPECT_CHECK_LABEL)).toHaveLength(31);
  });
});

describe("run kinds", () => {
  it("include the two plugin check kinds", () => {
    expect(RUN_KINDS).toContain("plugin_inspect");
    expect(RUN_KINDS).toContain("plugin_describe");
    expect(PLUGIN_RUN_KINDS).toEqual(["plugin_inspect", "plugin_describe"]);
    expect(isPluginRunKind("fit")).toBe(false);
  });
});

// ------------------------------------------------------------------------------ the mock

describe("plugin fixtures (mock)", () => {
  it("hold a version in every state for a tenant admin", async () => {
    const all = [...(await versions()), ...(await versions("ana", PLUGIN_GEO))];
    expect(new Set(all.map((v) => v.state))).toEqual(new Set(PLUGIN_VERSION_STATES));
    const plugins = (await call<{ items: Plugin[] }>("ana", "GET", "/v1/plugins")).data.items;
    expect(plugins.map((p) => p.namespace).sort()).toEqual([ACME_NS, "t_k3vq7m_geo"]);
    expect(plugins.find((p) => p.id === PLUGIN_ACME)?.versions).toBe(9);
  });

  it("hide other people's unapproved versions from a viewer", async () => {
    const seen = await versions("vic");
    expect(seen.map((v) => v.state).sort()).toEqual(["approved", "yanked"]);
    expect((await call("vic", "GET", `/v1/plugins/${PLUGIN_ACME}/versions/${V.privateOwn}`)).status).toBe(404);
    expect((await call("vic", "GET", `/v1/plugins/${PLUGIN_GEO}`)).status).toBe(404);
  });

  it("show a refused inspect with five problems and no SBOM", async () => {
    const v = await version(V.refused);
    expect(isRefused(v)).toBe(true);
    expect(v.inspection?.ok).toBe(false);
    expect(v.inspection?.problems).toHaveLength(5);
    expect(v.inspection?.sbom).toBeNull();
    expect(v.overlay_sha256).toBeNull();
  });

  it("show a kit report with pass, fail and skipped (failures first)", async () => {
    const v = await version(V.kitFailed);
    expect(kitCounts(v.kit)).toEqual({ pass: 3, fail: 2, skipped: 1 });
    expect(groupKit(v.kit).map((g) => g.status)).toEqual(["fail", "pass", "skipped"]);
  });

  it("give the reviewer an SBOM of 40 files, locked requirements and every About URL", async () => {
    const v = await version(V.awaiting);
    const s = sbomSummary(v.inspection?.sbom);
    expect(s?.files).toBe(40);
    expect(s?.unlocked).toBe(0);
    expect(s?.requirements.map((r) => r.locked_version)).toEqual(["2.1.3", "2.2.3", "1.14.1"]);
    expect(v.urls).toContain("javascript:alert(1)");
    expect((v.urls ?? []).filter((u) => referenceLink(u).ok)).toEqual(["https://example.org/acme", "https://xn--bcher-kva.example/docs"]);
    expect(v.exports_detail?.steps?.[0]?.about?.title).toContain("<img");
    expect(exportsSummary(v)).toBe("1 model, 1 preparation step, 1 chart");
  });

  it("mark private and tenant-wide versions", async () => {
    expect(isPrivate(await version(V.privateOwn))).toBe(true);
    expect((await version(V.approved)).scope).toBe("tenant");
    expect((await version(V.refused)).scope).toBe("none");
  });

  it("refuse a public workspace and a deployment with uploads turned off", async () => {
    expect((await call("ana", "GET", "/v1/plugins", undefined, { "X-Mapi-Project": "globex/sandbox" })).status).toBe(403);
    localStorage.setItem(MOCK_PLUGINS_OFF_KEY, "1");
    expect(mockPluginFlags()).toEqual({ plugins_enabled: false, dev_untrusted_code: false, plugins_paused: false });
    expect((await call("ana", "GET", "/v1/plugins")).status).toBe(403);
    localStorage.setItem(MOCK_DEV_UNTRUSTED_KEY, "1");
    expect(mockPluginFlags().dev_untrusted_code).toBe(true);
  });
});

describe("claims and uploads (mock)", () => {
  it("let admins and attested editors claim; refuse viewers and taken names", async () => {
    expect((await call("vic", "POST", "/v1/plugins", { short_name: "vic", title: "Vic" })).status).toBe(403);
    const rui = await call<Plugin>("rui", "POST", "/v1/plugins", { short_name: "rui", title: "Rui tools" });
    expect(rui.status).toBe(201);
    expect(rui.data.namespace).toBe("t_k3vq7m_rui");
    expect((await call("ana", "POST", "/v1/plugins", { short_name: "acme", title: "Again" })).data).toMatchObject({ code: "PLUGIN_NAMESPACE_TAKEN" });
    expect((await call("ana", "POST", "/v1/plugins", { short_name: "a_b", title: "Bad" })).status).toBe(422);
  });

  it("move a browser upload inspecting → describing → kit_passed", async () => {
    localStorage.setItem(MOCK_PLUGIN_MS_KEY, "1200");
    const phases: string[] = [];
    const v = await uploadWheel(scope("ana"), { pluginId: PLUGIN_ACME, file: wheel(`${ACME_NS}-0.4.0-py3-none-any.whl`), onPhase: (p) => phases.push(p) });
    expect(phases).toEqual(["ticket", "sending", "completing", "done"]);
    expect(v.state).toBe("inspecting");
    expect(v.uploaded_by).toBe(ANA_ID);
    // The files check takes the first 37.5 % of the time (450 ms), the tests the rest.
    await new Promise((r) => setTimeout(r, 600));
    expect((await version(v.id)).state).toBe("describing");
    await new Promise((r) => setTimeout(r, 700));
    const done = await version(v.id);
    expect(done.state).toBe("kit_passed");
    expect(done.scope).toBe("private");
    expect(done.kit?.ok).toBe(true);
  });

  it("end a refused file in inspect_failed and refuse a second upload of the same version", async () => {
    localStorage.setItem(MOCK_PLUGIN_MS_KEY, "0");
    const v = await uploadWheel(scope("ana"), { pluginId: PLUGIN_ACME, file: wheel("acme_refused-0.5.0-py3-none-any.whl") });
    expect((await version(v.id)).state).toBe("inspect_failed");
    const failing = await uploadWheel(scope("ana"), { pluginId: PLUGIN_ACME, file: wheel(`${ACME_NS}-0.6.0.failing-py3-none-any.whl`) });
    expect((await version(failing.id)).state).toBe("kit_failed");
    await expect(uploadWheel(scope("ana"), { pluginId: PLUGIN_ACME, file: wheel(`${ACME_NS}-0.1.0-py3-none-any.whl`) })).rejects.toMatchObject({ code: "PLUGIN_VERSION_EXISTS" });
  });

  it("refuse a ticket for a file over 20 MiB", async () => {
    const res = await call("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/uploads`, { filename: `${ACME_NS}-0.9.0-py3-none-any.whl`, size: 21_000_000 });
    expect(res.status).toBe(413);
  });
});

describe("review (mock)", () => {
  it("lets a second admin approve with the page's digests; the catalog view then lists the entries", async () => {
    const v = await version(V.awaiting);
    expect(isDecidable(v)).toBe(true);
    if (!isDecidable(v)) return;
    expect(decisionBlockReason(v, meOf("ana"), "acme")).toBeNull();
    const before = (await call<CatalogResponse>("vic", "GET", "/v1/catalog")).data;
    expect(uploadedCatalogEntries(before).map((e) => e.versionId)).toEqual([V.approved, V.approved, V.approved]);
    const res = await call<PluginVersion>("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaiting}:approve`, decisionBody(v));
    expect(res.status).toBe(200);
    expect(res.data.state).toBe("approved");
    expect(res.data.approvals?.at(-1)).toMatchObject({ decision: "approve", approver_id: ANA_ID });
    const after = (await call<CatalogResponse>("vic", "GET", "/v1/catalog")).data;
    expect(new Set(uploadedCatalogEntries(after).map((e) => e.versionId))).toEqual(new Set([V.awaiting]));
  });

  it("refuses self-approval, stale digests, a missing note and a non-admin", async () => {
    const own = await version(V.awaitingOwn);
    if (!isDecidable(own)) throw new Error("fixture");
    expect(decisionBlockReason(own, meOf("ana"), "acme")).toMatch(/You uploaded this version/);
    expect((await call("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaitingOwn}:approve`, decisionBody(own))).data).toMatchObject({ code: "SELF_APPROVAL_FORBIDDEN" });
    const v = await version(V.awaiting);
    if (!isDecidable(v)) throw new Error("fixture");
    const stale = await call("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaiting}:approve`, { ...decisionBody(v), describe_digest: `sha256:${"0".repeat(64)}` });
    expect(stale).toMatchObject({ status: 409, data: { code: "PLUGIN_STATE_CONFLICT" } });
    expect((await call("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaiting}:reject`, decisionBody(v))).status).toBe(422);
    expect(decisionBlockReason(v, meOf("rui"), "acme")).toMatch(/Only workspace admins/);
    expect(decisionBlockReason(v, meOf("ana", "token"), "acme")).toMatch(/API tokens/);
    // Rui cannot see Bea's unapproved version at all, and may not decide on a visible one.
    expect((await call("rui", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaiting}:approve`, decisionBody(v))).status).toBe(404);
    expect((await call("rui", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.approved}:approve`, decisionBody(v))).status).toBe(403);
    const rejected = await call<PluginVersion>("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaiting}:reject`, decisionBody(v, "Remove the outside call."));
    expect(rejected.data.state).toBe("rejected");
  });

  it("asks for a fresh sign-in, which the mock identity provider answers", async () => {
    const v = await version(V.awaiting);
    if (!isDecidable(v)) throw new Error("fixture");
    localStorage.setItem(MOCK_PLUGIN_REAUTH_KEY, "1");
    const res = await call("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaiting}:approve`, decisionBody(v));
    expect(res).toMatchObject({ status: 401, data: { code: "REAUTH_REQUIRED" } });
    await fetch(`${env.authBase}/__mock/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ persona: "ana" }) });
    expect(localStorage.getItem(MOCK_PLUGIN_REAUTH_KEY)).toBeNull();
    expect((await call("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.awaiting}:approve`, decisionBody(v))).status).toBe(200);
  });

  it("requests approval, yanks with usage counts, and knows who may", async () => {
    const own = await version(V.privateOwn);
    expect(canRequestApproval(own, meOf("ana"), "acme")).toBe(true);
    expect(canRequestApproval(own, meOf("vic"), "acme")).toBe(false);
    expect(canYank(own, meOf("ana"), "acme")).toBe(true);
    expect(canYank(own, meOf("ana", "token"), "acme")).toBe(false);
    const asked = await call<PluginVersion>("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.privateOwn}:request-approval`);
    expect(asked.data.state).toBe("awaiting_approval");
    expect((await call<{ runs_30d: number }>("ana", "GET", `/v1/plugins/${PLUGIN_ACME}/versions/${V.approved}/usage`)).data.runs_30d).toBe(14);
    const yanked = await call<PluginVersion>("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.approved}:yank`, { reason: "security", note: "Bad call" });
    expect(yanked.data).toMatchObject({ state: "yanked", yank_reason: "security", scope: "none" });
    expect((await call("ana", "POST", `/v1/plugins/${PLUGIN_ACME}/versions/${V.refused}:yank`, { reason: "withdrawn" })).status).toBe(409);
  });

  it("shows a chart example of an uploaded recipe", async () => {
    const v = await version(V.approved);
    const plotId = v.exports_detail?.plots?.[0]?.id ?? "";
    const res = await call<{ id: string; title: string }>("vic", "GET", `/v1/plugins/${PLUGIN_ACME}/versions/${V.approved}/examples/${plotId}`);
    expect(res.status).toBe(200);
    expect(res.data.id).toBe(plotId);
  });
});

describe("tenant catalog view (mock)", () => {
  const uploaded = async (who: Who, headers: Record<string, string> = {}) =>
    new Set(uploadedCatalogEntries((await call<CatalogResponse>(who, "GET", "/v1/catalog", undefined, headers)).data).map((e) => e.versionId));

  it("needs the tenant: no X-Mapi-Project for a two-tenant user means the base catalog", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    const res = await fetch(`${env.apiBase}/v1/catalog`);
    const body = (await res.json()) as CatalogResponse;
    expect(uploadedCatalogEntries(body)).toEqual([]);
    // The newest usable version of a namespace wins: Ana sees her private 0.3.0.
    expect(await uploaded("ana")).toEqual(new Set([V.privateOwn]));
  });

  it("keeps private entries to their uploader and drops them when private use is off", async () => {
    expect(await uploaded("rui")).toEqual(new Set([V.approved]));
    const patched = await call<PluginSettings>("ana", "PATCH", "/v1/tenants/acme/plugin-settings", { private_use: false });
    expect(patched.data).toMatchObject({ private_use: false, tenant_code: "k3vq7m", uploads_allowed: true });
    expect(await uploaded("ana")).toEqual(new Set([V.approved]));
  });
});

describe("tenant settings (mock)", () => {
  it("are for tenant admins; attest and revoke uploaders, never yourself", async () => {
    expect((await call("rui", "GET", "/v1/tenants/acme/plugin-settings")).status).toBe(403);
    const list = await call<VerifiedUploader[]>("ana", "GET", "/v1/tenants/acme/plugin-uploaders");
    expect(list.data.map((u) => u.user_id)).toEqual([PERSONAS.rui!.id]);
    expect((await call("ana", "PUT", `/v1/tenants/acme/plugin-uploaders/${ANA_ID}`)).status).toBe(403);
    expect((await call<VerifiedUploader>("ana", "PUT", `/v1/tenants/acme/plugin-uploaders/${PERSONAS.vic!.id}`)).data.method).toBe("admin_attested");
    expect((await call("ana", "DELETE", `/v1/tenants/acme/plugin-uploaders/${PERSONAS.rui!.id}`)).status).toBe(204);
    expect((await call<VerifiedUploader[]>("ana", "GET", "/v1/tenants/acme/plugin-uploaders")).data.map((u) => u.user_id)).toEqual([PERSONAS.vic!.id]);
    // Rui is no longer attested: he cannot claim.
    expect((await call("rui", "POST", "/v1/plugins", { short_name: "rui", title: "Rui" })).status).toBe(403);
  });
});

describe("api helpers", () => {
  it("read the /v1/me flags with their defaults and classify problems", () => {
    expect(pluginFlags(undefined)).toEqual({ enabled: true, devUntrusted: false, paused: false });
    expect(pluginFlags({ ...meOf("ana"), plugins_enabled: false, dev_untrusted_code: true })).toEqual({ enabled: false, devUntrusted: true, paused: false });
    expect(pluginFlags({ ...meOf("ana"), plugins_paused: true }).paused).toBe(true);
    expect(isReauthRequired(new Error("x"))).toBe(false);
    expect(isStateConflict(null)).toBe(false);
    expect(pk.version("acme/growth", "plg", "plv")).toEqual(["p", "acme/growth", "plugins", "item", "plg", "versions", "plv"]);
    expect(BEA_ID).not.toBe(ANA_ID);
  });
});

describe("stateDescription (v0.19 live)", () => {
  it("does not say tests failed when only the description of the exports was refused", async () => {
    const v = await version(V.kitFailed);
    expect(stateDescription(v)).toBe(STATE_DESCRIPTION.kit_failed);
    const passing = { ...v, kit: { ok: true, checks: (v.kit?.checks ?? []).map((c) => ({ ...c, status: "pass" as const })) } };
    const issue = { code: "PLUGIN_REFUSED", severity: "error", pointer: "/exports/steps/0/about/title", message: "The text holds hidden characters." };
    expect(stateDescription({ ...passing, issues: [issue] } as PluginVersion)).toBe(KIT_FAILED_ISSUES_DESCRIPTION);
    expect(stateDescription({ ...passing, issues: [] })).toBe(STATE_DESCRIPTION.kit_failed);
    expect(stateDescription({ ...passing, state: "kit_passed", issues: [issue] } as PluginVersion)).toBe(STATE_DESCRIPTION.kit_passed);
  });
});
