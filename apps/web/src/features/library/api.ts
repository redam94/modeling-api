/**
 * TanStack Query hooks of the library (`/v1/library`, v0.18 I-5): list, read, create,
 * revise (`If-Match` with the item's `ETag`), archive, promote, validate, use, and the
 * JSON-only chart template preview.
 *
 * Every route is project-scoped: keys start with `["p", "<tenant>/<project>", "library"]`
 * (`keys.ts`) and every request names the same project in `X-Mapi-Project`. A save emits
 * the `library_item_saved` onboarding event (the checklist's "Make your first model
 * recipe" step listens for it).
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, unwrap } from "@/api/client";
import { useProjectScope, type ProjectScope } from "@/api/queries";
import { everyWord } from "@/features/search/rank";
import { MAX_LIBRARY_QUERY_CHARS } from "@/lib/list-query";
import { emitOnboarding } from "@/lib/onboarding-events";

import { lk } from "./keys";
import type {
  ChartPreview,
  JsonObject,
  LibraryCreate,
  LibraryItem,
  LibraryKind,
  LibraryListFilters,
  LibraryRevision,
  LibraryRevisionCreate,
  LibraryUseRequest,
  LibraryUseResult,
  LibraryValidation,
} from "./types";

const LIMIT = 200;
/** 500 items per tenant at most (`MAX_ITEMS_PER_TENANT`): three pages cover them. */
const MAX_PAGES = 5;

/** A stable key for a document (canonical JSON: object keys sorted). */
export function docKey(value: unknown): string {
  const seen = new WeakSet<object>();
  const canon = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(canon);
    if (typeof v === "object" && v !== null) {
      if (seen.has(v)) return null;
      seen.add(v);
      const o = v as Record<string, unknown>;
      return Object.fromEntries(
        Object.keys(o)
          .sort()
          .filter((k) => o[k] !== undefined)
          .map((k) => [k, canon(o[k])]),
      );
    }
    return v;
  };
  return JSON.stringify(canon(value)) ?? "";
}

function definedQuery(f: LibraryListFilters) {
  return {
    ...(f.kind ? { kind: f.kind } : {}),
    ...(f.scope ? { scope: f.scope } : {}),
    // The API takes 120 characters of text: a longer text sends its start (see `fetchLibraryItems`).
    ...(f.q?.trim() ? { q: f.q.trim().slice(0, MAX_LIBRARY_QUERY_CHARS).trim() } : {}),
    ...(f.include_archived ? { include_archived: true } : {}),
  };
}

/** Every item of the project and the tenant that matches `f` (all pages). */
export async function fetchLibraryItems(s: ProjectScope, f: LibraryListFilters, signal?: AbortSignal): Promise<LibraryItem[]> {
  const out: LibraryItem[] = [];
  let cursor: string | null | undefined;
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await unwrap(
      api.GET("/v1/library", {
        params: { query: { limit: LIMIT, ...definedQuery(f), ...(cursor ? { cursor } : {}) } },
        headers: s.headers,
        ...(signal ? { signal } : {}),
      }),
    );
    out.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) break;
  }
  // The server saw only the start of a long text: keep the items that have each word of it.
  const q = f.q?.trim() ?? "";
  return q.length > MAX_LIBRARY_QUERY_CHARS ? out.filter((i) => everyWord(q, [i.title, i.name])) : out;
}

/** The library items of the project (and the tenant's), most recently changed first. */
export function useLibraryItems(f: LibraryListFilters = {}, opts: { enabled?: boolean } = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: lk.list(s.key, f),
    queryFn: ({ signal }) => fetchLibraryItems(s, f, signal),
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[1] === s.key ? prev : undefined),
    enabled: (opts.enabled ?? true) && s.key !== "-",
  });
}

/** The non-archived items of one kind (pickers: "From your team" in Build, the gallery, …). */
export function useLibraryPicker(kind: LibraryKind, enabled = true) {
  return useLibraryItems({ kind }, { enabled });
}

/** One item (its `etag` is what a new revision sends as `If-Match`). */
export function useLibraryItem(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: lk.item(s.key, id ?? ""),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/library/{item_id}", { params: { path: { item_id: id ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(id),
  });
}

/** The revisions of an item, newest first (one page of 200: the cap per item). */
export function useLibraryRevisions(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: lk.revisions(s.key, id ?? ""),
    queryFn: async ({ signal }): Promise<LibraryRevision[]> =>
      (await unwrap(api.GET("/v1/library/{item_id}/revisions", { params: { path: { item_id: id ?? "" }, query: { limit: LIMIT } }, headers: s.headers, signal }))).items,
    enabled: Boolean(id),
  });
}

/** One revision (the latest when `revision` is `"latest"`, read through the item). */
export function useLibraryRevision(id: string | undefined, revision: number | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: lk.revision(s.key, id ?? "", revision ?? 0),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/library/{item_id}/revisions/{revision}", { params: { path: { item_id: id ?? "", revision: revision ?? 1 } }, headers: s.headers, signal })),
    enabled: Boolean(id) && revision !== undefined && revision >= 1,
    staleTime: Infinity, // revisions are immutable
  });
}

function useInvalidateLibrary(s: ProjectScope) {
  const qc = useQueryClient();
  return async (item?: LibraryItem) => {
    if (item) qc.setQueryData(lk.item(s.key, item.id), item);
    await Promise.all([
      qc.invalidateQueries({ queryKey: lk.lists(s.key) }),
      item ? qc.invalidateQueries({ queryKey: lk.revisions(s.key, item.id) }) : Promise.resolve(),
    ]);
  };
}

/** Save a new item (revision 1). Invalid documents are stored with `valid: false` (except workflow templates: 422). */
export function useCreateLibraryItem() {
  const s = useProjectScope();
  const invalidate = useInvalidateLibrary(s);
  return useMutation({
    mutationFn: (body: LibraryCreate) => unwrap(api.POST("/v1/library", { headers: s.headers, body })),
    onSuccess: async (item) => {
      emitOnboarding("library_item_saved", { kind: item.kind, item_id: item.id });
      await invalidate(item);
    },
  });
}

/** The 412 of a revision whose `If-Match` is stale (someone saved in between). */
export function isStaleRevision(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 412 || e.code === "PRECONDITION_FAILED");
}

/** Save a new revision. `etag` is the item's `etag` you edited (412 when it moved: reload and merge). */
export function useReviseLibraryItem(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  const invalidate = useInvalidateLibrary(s);
  return useMutation({
    mutationFn: ({ body, etag }: { body: LibraryRevisionCreate; etag?: string | null }) =>
      unwrap(
        api.POST("/v1/library/{item_id}/revisions", {
          params: { path: { item_id: id } },
          headers: { ...s.headers, ...(etag ? { "If-Match": etag } : {}) },
          body,
        }),
      ),
    onSuccess: async (rev) => {
      emitOnboarding("library_item_saved", { item_id: id, revision: rev.revision });
      qc.setQueryData(lk.revision(s.key, id, rev.revision), rev);
      await Promise.all([invalidate(), qc.invalidateQueries({ queryKey: lk.item(s.key, id) })]);
    },
    onError: async (e) => {
      if (isStaleRevision(e)) await qc.invalidateQueries({ queryKey: lk.item(s.key, id) });
    },
  });
}

type ItemAction = "archive" | "unarchive" | "promote";

function itemAction(s: ProjectScope, id: string, action: ItemAction): Promise<LibraryItem> {
  const params = { params: { path: { item_id: id } }, headers: s.headers };
  if (action === "archive") return unwrap(api.POST("/v1/library/{item_id}:archive", params));
  if (action === "unarchive") return unwrap(api.POST("/v1/library/{item_id}:unarchive", params));
  return unwrap(api.POST("/v1/library/{item_id}:promote", params));
}

/** Archive (hide from lists and pickers; nothing is deleted) or show an item again. */
export function useArchiveLibraryItem() {
  const s = useProjectScope();
  const invalidate = useInvalidateLibrary(s);
  return useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) => itemAction(s, id, archived ? "archive" : "unarchive"),
    onSuccess: (item) => invalidate(item),
  });
}

/** Copy a project item's latest revision into a new tenant item (tenant admins). */
export function usePromoteLibraryItem() {
  const s = useProjectScope();
  const invalidate = useInvalidateLibrary(s);
  return useMutation({
    mutationFn: (id: string) => itemAction(s, id, "promote"),
    onSuccess: (item) => invalidate(item),
  });
}

export interface ValidateInput {
  /** The document, or YAML/JSON `text` (exactly one). */
  document?: JsonObject | null;
  text?: string | null;
  /** `dataset://…` adds binding, shape, slot and variable checks. */
  dataset?: string | null;
}

/** `POST /v1/library:validate` (nothing is saved; issues point into the document). */
export function validateLibraryDoc(s: ProjectScope, input: ValidateInput, signal?: AbortSignal): Promise<LibraryValidation> {
  const body = {
    ...(input.document ? { document: input.document } : {}),
    ...(input.text != null ? { text: input.text } : {}),
    ...(input.dataset ? { dataset: input.dataset } : {}),
  };
  return unwrap(api.POST("/v1/library:validate", { headers: s.headers, body, ...(signal ? { signal } : {}) }));
}

/**
 * The validation of `document` (debounce the document before passing it: the route allows
 * 60 calls a minute). Keyed by the document's canonical JSON, so an unchanged document is
 * not checked twice.
 */
export function useLibraryValidation(document: JsonObject | null, dataset: string | null = null, enabled = true) {
  const s = useProjectScope();
  const key = document ? docKey(document) : "";
  return useQuery({
    queryKey: lk.validation(s.key, key, dataset),
    queryFn: ({ signal }) => validateLibraryDoc(s, { document, dataset }, signal),
    enabled: enabled && document !== null,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    retry: false,
  });
}

/** `POST /v1/library/{id}:use`: compile an item for use (a spec fragment, steps, a chart or a workflow document). */
export function useLibraryItemUse(id: string) {
  const s = useProjectScope();
  return useMutation({
    mutationFn: async (body: LibraryUseRequest): Promise<LibraryUseResult> =>
      (await unwrap(api.POST("/v1/library/{item_id}:use", { params: { path: { item_id: id } }, headers: s.headers, body }))) as LibraryUseResult,
  });
}

/** `POST /v1/library/charts:preview`: a chart template on its source recipe's example data (JSON only, no run). */
export async function previewChartTemplate(s: ProjectScope, document: JsonObject, signal?: AbortSignal): Promise<ChartPreview> {
  return (await unwrap(api.POST("/v1/library/charts:preview", { headers: s.headers, body: { document }, ...(signal ? { signal } : {}) })));
}

/** The live preview of a chart template document (debounce the document first: 60 calls a minute). */
export function useChartPreview(document: JsonObject | null, enabled = true) {
  const s = useProjectScope();
  const key = document ? docKey(document) : "";
  return useQuery({
    queryKey: lk.chartPreview(s.key, key),
    queryFn: ({ signal }) => previewChartTemplate(s, document ?? {}, signal),
    enabled: enabled && document !== null,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    retry: false,
  });
}
