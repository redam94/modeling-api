/**
 * Data of the "extend" surfaces (v0.18 D12-D15): the catalog diagnostics tenant admins see
 * in Catalog › Yours › Problems, the operator's "describe again" action, the catalog
 * entries a team installed (`provider` set: "Installed by your team"), and the exact
 * `mapi plugin init` command the "Add your own" panel copies.
 *
 * Diagnostics carry `untrusted_text` from a plugin: render it as text only (a clipped
 * monospace `<pre>`), never as markup.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, unwrap } from "@/api/client";
import { qk, useCatalog } from "@/api/queries";
import type { components } from "@/api/schema";
import type { CatalogResponse, Me } from "@/api/types";

export type CatalogDiagnostic = components["schemas"]["CatalogDiagnostic"];
export type DiagnosticCode = CatalogDiagnostic["code"];

/** Plain names of the diagnostic codes (reference text: STE). */
export const DIAGNOSTIC_LABEL: Record<DiagnosticCode, string> = {
  load_error: "Did not load",
  namespace_reserved: "Name is reserved",
  id_collision: "Same id as another entry",
  schema_unsafe: "Unsafe settings schema (part removed)",
  about_incomplete: "About card is not complete",
  node_contract_invalid: "Workflow step is not valid",
  template_invalid: "Workflow template is not valid",
  deprecated: "Uses an old format",
};

export const extendKeys = {
  diagnostics: ["catalog", "diagnostics"] as const,
};

/** Operators and tenant admins (the server refuses everyone else with 403). */
export function canSeeDiagnostics(me: Me | null | undefined): boolean {
  if (!me) return false;
  return Boolean(me.operator) || (me.tenants ?? []).some((t) => t.role === "admin");
}

/** Problems found while the current catalog was built (tenant admins and operators only). */
export function useCatalogDiagnostics(enabled: boolean) {
  return useQuery({
    queryKey: extendKeys.diagnostics,
    queryFn: async ({ signal }): Promise<CatalogDiagnostic[]> => unwrap(api.GET("/v1/catalog/diagnostics", { signal })),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
  });
}

/** Diagnostics by distribution (`null`: not from one plugin), in the order the server sent them. */
export function diagnosticsByDistribution(items: readonly CatalogDiagnostic[]): Map<string | null, CatalogDiagnostic[]> {
  const out = new Map<string | null, CatalogDiagnostic[]>();
  for (const d of items) {
    const k = d.distribution ?? null;
    const list = out.get(k);
    if (list) list.push(d);
    else out.set(k, [d]);
  }
  return out;
}

/** Operators: queue a `describe` run for the current image digest (no-op when one is pending). */
export function useRequestDescribe() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/admin/catalog:describe", {})),
    onSuccess: () => Promise.all([qc.invalidateQueries({ queryKey: qk.catalog }), qc.invalidateQueries({ queryKey: extendKeys.diagnostics })]),
  });
}

// ---------------------------------------------------------------------- installed by your team

export type ExtensionKind = "model" | "step" | "plot";

export interface InstalledExtension {
  kind: ExtensionKind;
  id: string;
  version: string;
  title: string;
  /** The distribution that installed it (`provider`). */
  provider: string;
}

/** The catalog entries a non-built-in distribution provides (D13), grouped by nothing: catalog order. */
export function installedExtensions(catalog: CatalogResponse | undefined): InstalledExtension[] {
  if (!catalog) return [];
  const out: InstalledExtension[] = [];
  for (const m of catalog.models ?? []) {
    if (m.provider) out.push({ kind: "model", id: m.manifest.id, version: m.manifest.version, title: m.manifest.about?.title ?? m.manifest.id, provider: m.provider });
  }
  for (const s of catalog.steps ?? []) {
    if (s.provider) out.push({ kind: "step", id: s.id, version: s.version, title: s.about?.title ?? s.id, provider: s.provider });
  }
  for (const p of catalog.plots ?? []) {
    if (p.provider) out.push({ kind: "plot", id: p.id, version: p.version ?? "", title: p.title, provider: p.provider });
  }
  return out;
}

/** "Installed by your team": the catalog's plugin entries. */
export function useInstalledExtensions() {
  const catalog = useCatalog();
  const items = useMemo(() => installedExtensions(catalog.data), [catalog.data]);
  return { ...catalog, items };
}

// ---------------------------------------------------------------------- the developer track (D15)

/** What `mapi plugin init --kind` makes. */
export const PLUGIN_KINDS = ["model", "step", "recipe", "node", "template"] as const;
export type PluginKind = (typeof PLUGIN_KINDS)[number];

/** The `--kind` for each "Add your own" panel. */
export const PANEL_PLUGIN_KIND = {
  model: "model",
  step: "step",
  plot: "recipe",
  workflow_node: "node",
  workflow_template: "template",
} as const satisfies Record<string, PluginKind>;

/** The exact command the "Add your own" panel copies (`name` is a placeholder the user edits). */
export function pluginInitCommand(kind: PluginKind, name = "acme"): string {
  return `mapi plugin init ${name} --kind ${kind}`;
}

