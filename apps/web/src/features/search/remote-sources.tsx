/**
 * The remote sources of the global search (v0.21), a lazy chunk the command palette loads on
 * the first query. Each source is one small component that renders nothing: it reads one list
 * and reports its candidates, so a slow or failed list never holds the others.
 *
 * - Runs, experiments and "Make your own" filter on the server (`name`, `name`, `q`).
 * - Datasets, models, specs, dashboards, workflows and endpoints have no text filter: one page
 *   of 200 (the maximum), under the query keys of the list pages, and the palette filters it.
 * - Catalog: the About cards, filtered by the palette.
 *
 * The keys of other features are written out here and not imported: an import would move their
 * modules between chunks (the share link and the portal have byte budgets). `keys.test.ts`
 * holds them equal to the real ones.
 */
import { keepPreviousData, useQuery, type QueryKey } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";

import { api, unwrap } from "@/api/client";
import { fetchExperiments } from "@/api/experiments";
import { qk, useDatasets, useModels, useProjectScope, useRuns, useSpecs } from "@/api/queries";
import { sentenceCase as sentence, type Candidate, type RemoteGroupId, type SourceState } from "./kinds";

/** The fields the search reads of each list item (the lists' own types are wider). */
interface Page<T> {
  items: T[];
}
interface Named {
  id: string;
  name: string;
  updated_at: string;
}
type DatasetRow = Named & { description?: string | null };
type ModelRow = Named & { latest_version?: number | null };
type SpecRow = Named & { latest_revision: number };
type WorkflowRow = Named & { description?: string | null };
type DashboardRow = Named & { title: string; status: string; client_name?: string | null };
interface EndpointRow {
  id: string;
  alias: string;
  model_ref: string;
  updated_at: string;
}
interface AboutRow {
  kind: string;
  id: string;
  about: { title: string };
}
interface RunRow {
  id: string;
  name?: string | null;
  kind: string;
  state: string;
  created_at: string;
}
type ExperimentRow = Named & { run_count?: number | null; last_run_at?: string | null };
type LibraryRow = Named & { title: string; kind: string };

/** Rows the server filters: a little more than the palette shows is not needed. */
export const SERVER_ROWS = 5;
/** One page of a list without a text filter (the API's maximum). */
export const LIST_PAGE = 200;

export const searchKeys = {
  /** = `dk.list(p)` (features/dashboards/keys.ts). */
  dashboards: (p: string) => ["p", p, "dashboards", "list", {}] as const,
  /** = the key of `useWorkflows()` (features/workflows/api.ts). */
  workflows: (p: string) => ["p", p, "workflows", { archived: false }] as const,
  /** = `ik.endpoints(p)` (api/integrations.ts). */
  endpoints: (p: string) => ["p", p, "endpoints"] as const,
  /** = `aboutKeys.abouts` (features/about/api.ts): every About card, as that query stores them. */
  abouts: ["catalog", "abouts"] as const,
  /** Under `lk.lists(p)` (features/library/keys.ts), so a change to the library refreshes it. */
  library: (p: string, q: string) => ["p", p, "library", "list", { q, limit: SERVER_ROWS }] as const,
};

export type Report = (id: RemoteGroupId, state: SourceState | null) => void;

interface QueryLike<T> {
  data: T | undefined;
  isError: boolean;
  /** The data is of the query before (it stays while the new one loads). */
  isPlaceholderData: boolean;
}

const NONE: Candidate[] = [];

/** Tell the palette what `id` has now; forget it when the source goes away. */
function useReport<T>(id: RemoteGroupId, report: Report, q: QueryLike<T>, toItems: (data: T) => Candidate[]): null {
  const items = useMemo(() => (q.data === undefined ? NONE : toItems(q.data)), [q.data, toItems]);
  const status: SourceState["status"] = q.data !== undefined && !q.isPlaceholderData ? "ready" : q.isError ? "error" : "loading";
  useEffect(() => {
    report(id, { status, items });
  }, [id, report, status, items]);
  useEffect(() => () => report(id, null), [id, report]);
  return null;
}

// ------------------------------------------------------------------ lists the palette filters

const datasetItems = (page: Page<DatasetRow>): Candidate[] =>
  page.items.map((d) => ({ id: d.id, title: d.name, secondary: d.description || d.id, href: `/datasets/${d.id}`, at: d.updated_at }));

function Datasets({ report }: { report: Report }) {
  return useReport("datasets", report, useDatasets(), datasetItems);
}

const modelItems = (page: Page<ModelRow>): Candidate[] =>
  page.items.map((m) => ({ id: m.id, title: m.name, secondary: m.latest_version ? `Version ${m.latest_version} · ${m.id}` : m.id, href: `/models/${m.id}`, at: m.updated_at }));

function Models({ report }: { report: Report }) {
  return useReport("models", report, useModels(), modelItems);
}

const specItems = (page: Page<SpecRow>): Candidate[] =>
  page.items.map((s) => ({ id: s.id, title: s.name, secondary: `Revision ${s.latest_revision} · ${s.id}`, href: `/build/${s.id}`, at: s.updated_at }));

function Specs({ report }: { report: Report }) {
  return useReport("specs", report, useSpecs(), specItems);
}

const dashboardItems = (page: Page<DashboardRow>): Candidate[] =>
  page.items.map((d) => ({
    id: d.id,
    title: d.title || d.name,
    secondary: [sentence(d.status), d.client_name].filter(Boolean).join(" · "),
    href: `/dashboards/${d.id}`,
    at: d.updated_at,
    alt: d.name,
  }));

function Dashboards({ report }: { report: Report }) {
  const s = useProjectScope();
  const q = useQuery({
    queryKey: searchKeys.dashboards(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/dashboards", { params: { query: { limit: LIST_PAGE } }, headers: s.headers, signal })),
  });
  return useReport("dashboards", report, q, dashboardItems);
}

const workflowItems = (page: Page<WorkflowRow>): Candidate[] =>
  page.items.map((w) => ({ id: w.id, title: w.name, secondary: w.description || w.id, href: `/workflows/${w.id}`, at: w.updated_at }));

function Workflows({ report }: { report: Report }) {
  const s = useProjectScope();
  const q = useQuery({
    queryKey: searchKeys.workflows(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/workflows", { params: { query: { limit: LIST_PAGE, archived: false } }, headers: s.headers, signal })),
  });
  return useReport("workflows", report, q, workflowItems);
}

const endpointItems = (page: Page<EndpointRow>): Candidate[] =>
  page.items.map((e) => ({ id: e.id, title: e.alias, secondary: e.model_ref, href: `/integrations/endpoints/${encodeURIComponent(e.alias)}`, at: e.updated_at }));

function Endpoints({ report }: { report: Report }) {
  const s = useProjectScope();
  const q = useQuery({
    queryKey: searchKeys.endpoints(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/endpoints", { params: { query: { limit: LIST_PAGE } }, headers: s.headers, signal })),
  });
  return useReport("endpoints", report, q, endpointItems);
}

/** `kind:id` as the catalog route accepts it in `?about=` (`router.tsx`). */
const ABOUT_KEY_RE = /^[a-z_]{1,32}:[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$/;

const aboutItems = (items: AboutRow[]): Candidate[] =>
  items
    .filter((a) => ABOUT_KEY_RE.test(`${a.kind}:${a.id}`))
    .map((a) => ({ id: a.id, title: a.about.title, secondary: `${sentence(a.kind)} · ${a.id}`, href: `/catalog?about=${a.kind}:${a.id}`, alt: a.id }));

/** Every page of the About cards, as `fetchAllAbouts` (features/about/api.ts) reads them. */
async function fetchAbouts(signal: AbortSignal): Promise<AboutRow[]> {
  const out: AboutRow[] = [];
  let cursor: string | null | undefined = undefined;
  for (let i = 0; i < 20; i++) {
    const page: { items: AboutRow[]; next_cursor?: string | null } = await unwrap(
      api.GET("/v1/catalog/abouts", { params: { query: { limit: LIST_PAGE, ...(cursor ? { cursor } : {}) } }, signal }),
    );
    out.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) break;
  }
  return out;
}

function Catalog({ report }: { report: Report }) {
  const q = useQuery({ queryKey: searchKeys.abouts, queryFn: ({ signal }) => fetchAbouts(signal), staleTime: 30 * 60_000 });
  return useReport("catalog", report, q, aboutItems);
}

// ------------------------------------------------------------------ lists the server filters

const runItems = (page: Page<RunRow>): Candidate[] =>
  page.items.map((r) => ({ id: r.id, title: r.name || r.id, secondary: `${sentence(r.kind)} · ${r.state} · ${r.id}`, href: `/runs/${r.id}`, at: r.created_at }));

function Runs({ query, report }: { query: string; report: Report }) {
  return useReport("runs", report, useRuns({ name: query, limit: SERVER_ROWS }, { live: false }), runItems);
}

/** The rows of the last query stay while the next one loads, but never another project's. */
const sameProject =
  (projectKey: string) =>
  <T,>(prev: T | undefined, prevQuery: { queryKey: QueryKey } | undefined): T | undefined =>
    prevQuery?.queryKey[1] === projectKey ? keepPreviousData(prev) : undefined;

const experimentItems = (page: Page<ExperimentRow>): Candidate[] =>
  page.items.map((e) => ({
    id: e.id,
    title: e.name,
    secondary: e.run_count === undefined || e.run_count === null ? e.id : `${e.run_count} ${e.run_count === 1 ? "run" : "runs"}`,
    href: `/experiments/${e.id}`,
    at: e.last_run_at ?? e.updated_at,
  }));

function Experiments({ query, report }: { query: string; report: Report }) {
  const s = useProjectScope();
  const q = useQuery({
    queryKey: qk.experimentList(s.key, { name: query }),
    queryFn: ({ signal }) => fetchExperiments({ name: query }, { headers: s.headers, signal }),
    placeholderData: sameProject(s.key),
  });
  return useReport("experiments", report, q, experimentItems);
}

/** A library id as the library route accepts it in `?item=` (`router.tsx`). */
const LIBRARY_ID_RE = /^lib_[0-9a-z]{26}$/;

const libraryItems = (items: LibraryRow[]): Candidate[] =>
  items
    .filter((i) => LIBRARY_ID_RE.test(i.id))
    .map((i) => ({ id: i.id, title: i.title, secondary: `${sentence(i.kind)} · ${i.name}`, href: `/library?item=${i.id}`, at: i.updated_at, alt: i.name }));

function Library({ query, report }: { query: string; report: Report }) {
  const s = useProjectScope();
  const q = useQuery({
    queryKey: searchKeys.library(s.key, query),
    queryFn: async ({ signal }) => (await unwrap(api.GET("/v1/library", { params: { query: { q: query, limit: SERVER_ROWS } }, headers: s.headers, signal }))).items,
    placeholderData: sameProject(s.key),
    enabled: s.key !== "-",
  });
  return useReport("library", report, q, libraryItems);
}

export interface RemoteSourcesProps {
  /** The text to ask the server for: normalised, two characters or more, after the pause. */
  query: string;
  /** The groups the caller may read (`remoteGroups` in `access.ts`): no other list is asked for. */
  groups: readonly RemoteGroupId[];
  report: Report;
}

export default function RemoteSources({ query, groups, report }: RemoteSourcesProps) {
  const on = (id: RemoteGroupId) => groups.includes(id);
  return (
    <>
      {on("datasets") ? <Datasets report={report} /> : null}
      {on("models") ? <Models report={report} /> : null}
      {on("specs") ? <Specs report={report} /> : null}
      {on("runs") ? <Runs query={query} report={report} /> : null}
      {on("experiments") ? <Experiments query={query} report={report} /> : null}
      {on("dashboards") ? <Dashboards report={report} /> : null}
      {on("workflows") ? <Workflows report={report} /> : null}
      {on("endpoints") ? <Endpoints report={report} /> : null}
      {on("catalog") ? <Catalog report={report} /> : null}
      {on("library") ? <Library query={query} report={report} /> : null}
    </>
  );
}
