/**
 * The science datasets of the mock API (`explore-sci.ts`, `explorer-sci.gen.json`): every
 * dataset, version, About card and ingest view is served, and an explore run finishes with
 * links to its views.
 */
import { setupServer } from "msw/node";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { env } from "@/env";

import { SCI, SCI_RUN_MS, sciDatasetId } from "./explore-sci";
import { createHandlers } from "./handlers";

const server = setupServer(...createHandlers());
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());

const API = env.apiBase;
const get = async (url: string) => {
  const res = await fetch(url);
  return { status: res.status, body: (await res.json()) as Record<string, unknown> };
};
/** An artifact through its signed link, as the app reads it. */
const artifact = async (id: string, name: string) => {
  const link = await get(`${API}/v1/datasets/${id}/versions/1/artifacts/${encodeURIComponent(name)}`);
  expect(link.status, name).toBe(200);
  return (await get(String(link.body.url))).body;
};

interface Ref {
  section: string;
  view: string;
  status?: string;
  plots?: string;
}

describe("science datasets in the mock API", () => {
  it("has the eleven export cases and the lens tree, with unique ids", () => {
    expect(SCI.map((d) => d.case)).toEqual(["e07", "e50", "e60", "e18", "e21", "e19", "e23", "e77", "e79", "e81", "e81b", "e80tree"]);
    expect(new Set(SCI.map((d) => d.dataset.id)).size).toBe(SCI.length);
    expect(SCI.every((d) => d.dataset.id === sciDatasetId(d.case))).toBe(true);
  });

  it("lists them after the other datasets", async () => {
    const { body } = await get(`${API}/v1/datasets`);
    const ids = (body.items as { id: string }[]).map((d) => d.id);
    expect(ids.slice(-SCI.length)).toEqual(SCI.map((d) => d.dataset.id));
  });

  it.each(SCI.map((d) => [d.case, d] as const))("serves %s: dataset, version, About and every ingest view", async (_case, d) => {
    const id = d.dataset.id;
    expect((await get(`${API}/v1/datasets/${id}`)).body.name).toBe(d.dataset.name);
    expect((await get(`${API}/v1/datasets/${id}/versions/1`)).body.ref).toBe(d.version.ref);
    expect(typeof (await get(`${API}/v1/datasets/${id}/about`)).body.summary_generated).toBe("string");
    const indexes = ["ui/explore/index.json"];
    if (d.version.layout === "tree") {
      const nodes = (await artifact(id, "ui/explore/nodes/index.json")) as { nodes: Record<string, { index: string }> };
      indexes.push(...Object.values(nodes.nodes).map((n) => n.index));
    }
    for (const name of indexes) {
      const index = (await artifact(id, name)) as { sections: Ref[] };
      expect(index.sections.length).toBeGreaterThan(0);
      for (const ref of index.sections.filter((s) => (s.status ?? "ok") === "ok")) {
        const view = await artifact(id, ref.view);
        expect(String(view.format)).toMatch(/^mapi\.data-explorer\./);
        if (ref.plots) expect(Array.isArray(await artifact(id, ref.plots))).toBe(true);
      }
    }
  });

  it("answers 404 for an artifact the ingest did not write", async () => {
    const res = await fetch(`${API}/v1/datasets/${sciDatasetId("e07")}/versions/1/artifacts/ui%2Fexplore%2Fnope.json`);
    expect(res.status).toBe(404);
  });

  it("runs an explore run of every section and serves its views when done", async () => {
    const id = sciDatasetId("e07");
    const res = await fetch(`${API}/v1/datasets/${id}/versions/1/explore`, { method: "POST", body: "{}", headers: { "content-type": "application/json" } });
    expect(res.status).toBe(202);
    const started = (await res.json()) as { run: { id: string }; done: boolean };
    expect(started.done).toBe(false);
    await new Promise((r) => setTimeout(r, SCI_RUN_MS + 50));
    const { body } = await get(`${API}/v1/explorations/${started.run.id}`);
    expect(body.done).toBe(true);
    const index = body.index as { sections: Ref[] };
    const ok = index.sections.filter((s) => (s.status ?? "ok") === "ok");
    const links = body.views as { name: string; url: string }[];
    for (const ref of ok) expect(links.map((l) => l.name)).toContain(ref.view);
    // the same request again: the cached run
    const again = await fetch(`${API}/v1/datasets/${id}/versions/1/explore`, { method: "POST", body: "{}", headers: { "content-type": "application/json" } });
    expect(again.status).toBe(200);
  });

  it("explores one node of the lens tree", async () => {
    const id = sciDatasetId("e80tree");
    const res = await fetch(`${API}/v1/datasets/${id}/versions/1/explore`, { method: "POST", body: JSON.stringify({ node: "/psf" }), headers: { "content-type": "application/json" } });
    const started = (await res.json()) as { run: { id: string } };
    await new Promise((r) => setTimeout(r, SCI_RUN_MS + 50));
    const { body } = await get(`${API}/v1/explorations/${started.run.id}`);
    expect((body.index as { node?: string }).node).toBe("/psf");
  });
});
