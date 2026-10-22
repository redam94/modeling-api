/**
 * What the builder's tabs read beside the working copy: the dashboard of the open builder page
 * (from the route, out of the query cache the page already filled) and the source catalog of
 * its saved draft (`GET …/sources`: the outputs, metrics, pictures and inputs that panels and
 * inputs bind to). The catalog follows the SAVED draft's source: after a source change it is
 * stale until the draft is saved.
 */
import { useParams } from "@tanstack/react-router";

import { useDashboard, useDashboardSources } from "../api";
import { isLayout2, type AnyDashboardSpec, type Dashboard, type SourceCatalog } from "../types";

/** The dashboard of the open builder page (`/dashboards/$dashboardId`), or `undefined`. */
export function useBuilderDashboard(): Dashboard | undefined {
  const params = useParams({ strict: false });
  const id = params.dashboardId ?? "";
  return useDashboard(id, { enabled: Boolean(id) }).data;
}

export interface BuilderCatalog {
  /** `undefined` while it loads; `null` when the app could not read it. */
  catalog: SourceCatalog | null | undefined;
  /** The working copy's source differs from the saved draft's (save to read the new one). */
  stale: boolean;
  error: unknown;
  retry: () => void;
}

const sameSource = (a: AnyDashboardSpec["source"], b: AnyDashboardSpec["source"]) => JSON.stringify(a) === JSON.stringify(b);

/** The source catalog of the builder's dashboard (layout 2 drafts only). */
export function useBuilderCatalog(spec: AnyDashboardSpec): BuilderCatalog {
  const d = useBuilderDashboard();
  const on = Boolean(d) && isLayout2(spec);
  const q = useDashboardSources(d?.id ?? "", { enabled: on });
  const catalog = !on ? undefined : q.isError ? null : q.data;
  return { catalog, stale: Boolean(d) && !sameSource(spec.source, d!.draft_spec.source), error: q.error, retry: () => void q.refetch() };
}
