import { describe, expect, it } from "vitest";

import type { Grants } from "@/api/access";
import { EXPERT_NAV, LOCKED_NAV, navForMode } from "@/components/shell/nav";

import { idJump, remoteGroups, visiblePages } from "./access";

const grants = (role: Grants["projectRole"], over: Partial<Grants> = {}): Grants => ({ projectRole: role, tenantRole: null, operator: false, tenantStatus: "active", ...over });
const paths = (mode: Parameters<typeof navForMode>[0]) => new Set(navForMode(mode).map((n) => n.to));
const ULID = "01jaf3d1a2b3c4d5e6f7g8h9j0";

describe("navForMode", () => {
  it("answers the list the sidebar shows in each mode", () => {
    expect(navForMode("expert")).toBe(EXPERT_NAV);
    expect(navForMode("locked")).toBe(LOCKED_NAV);
    expect(navForMode("guided").map((n) => n.label).slice(0, 3)).toEqual(["Guided", "Help me choose", "Recipes"]);
    expect(navForMode("guided").some((n) => n.to === "/")).toBe(false);
  });
});

describe("visiblePages", () => {
  it("keeps the pages a viewer can read", () => {
    expect(visiblePages(EXPERT_NAV, grants("viewer")).map((n) => n.to)).toEqual(EXPERT_NAV.map((n) => n.to));
  });

  it("drops the pages of data the caller cannot read", () => {
    expect(visiblePages(EXPERT_NAV, grants(null)).map((n) => n.to)).toEqual(["/", "/guided", "/choose"]);
  });
});

describe("remoteGroups", () => {
  it("gives a viewer in Expert mode each group, in the order of the results", () => {
    expect(remoteGroups(paths("expert"), grants("viewer")).map((g) => g.id)).toEqual([
      "datasets",
      "models",
      "specs",
      "runs",
      "experiments",
      "dashboards",
      "workflows",
      "endpoints",
      "catalog",
      "library",
    ]);
  });

  it("gives a Guided-locked member only the groups of the pages in the sidebar", () => {
    expect(remoteGroups(paths("locked"), grants("runner")).map((g) => g.id)).toEqual(["workflows", "endpoints"]);
  });

  it("gives no group to a member without the grant", () => {
    expect(remoteGroups(paths("expert"), grants(null))).toEqual([]);
    expect(remoteGroups(paths("expert"), grants("admin", { tenantStatus: "deleting" }))).toEqual([]);
    // a client can read dashboards only
    expect(remoteGroups(paths("expert"), grants("client")).map((g) => g.id)).toEqual(["dashboards"]);
  });
});

describe("idJump", () => {
  it.each([
    [`run_${ULID}`, `/runs/run_${ULID}`, "Open run"],
    [`ds_${ULID}`, `/datasets/ds_${ULID}`, "Open dataset"],
    [`spc_${ULID}`, `/build/spc_${ULID}`, "Open spec"],
    [`mdl_${ULID}`, `/models/mdl_${ULID}`, "Open model"],
    [`dsh_${ULID}`, `/dashboards/dsh_${ULID}`, "Open dashboard"],
    [`exp_${ULID}`, `/experiments/exp_${ULID}`, "Open experiment"],
    [`wf_${ULID}`, `/workflows/wf_${ULID}`, "Open workflow"],
    [`val_${ULID}`, `/validations/val_${ULID}`, "Open validation"],
    [`pol_${ULID}`, `/governance/policies/pol_${ULID}`, "Open policy"],
    [`whk_${ULID}`, `/integrations/webhooks/whk_${ULID}`, "Open webhook"],
    [`plg_${ULID}`, `/plugins/plg_${ULID}`, "Open uploaded code"],
    [`lib_${ULID}`, `/library?item=lib_${ULID}`, "Open library item"],
  ])("opens %s", (id, href, label) => {
    expect(idJump(id, paths("expert"), grants("admin"))).toMatchObject({ id, href, label: `${label} ${id}` });
    expect(idJump(`  ${id} `, paths("expert"), grants("admin"))?.href).toBe(href);
  });

  it("ignores text that is not an id", () => {
    expect(idJump("run_", paths("expert"), grants("admin"))).toBeNull();
    expect(idJump("runs", paths("expert"), grants("admin"))).toBeNull();
    expect(idJump(`xyz_${ULID}`, paths("expert"), grants("admin"))).toBeNull();
    expect(idJump(`run_${ULID}/../x`, paths("expert"), grants("admin"))).toBeNull();
  });

  it("does not open what the caller cannot read or what the sidebar hides", () => {
    expect(idJump(`whk_${ULID}`, paths("expert"), grants("viewer"))).toBeNull();
    expect(idJump(`run_${ULID}`, paths("expert"), grants(null))).toBeNull();
    expect(idJump(`run_${ULID}`, paths("locked"), grants("runner"))).toBeNull();
    expect(idJump(`wf_${ULID}`, paths("locked"), grants("runner"))?.href).toBe(`/workflows/wf_${ULID}`);
  });
});
