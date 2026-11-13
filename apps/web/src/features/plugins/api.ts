/**
 * TanStack Query hooks of uploaded code (`/v1/plugins…` and the tenant plugin settings,
 * v0.19 I-5): plugins and their versions, the browser upload (ticket → POST policy →
 * `:complete`), request approval, approve/reject with the page's digests, yank with usage
 * counts, recipe examples, and the tenant admin's settings and verified uploaders.
 *
 * The plugin routes act in the selected project (`X-Mapi-Project`, from the same render as
 * the key: `keys.ts`). The browser upload is `fetch(url, {method: "POST", body: FormData})` to
 * the quarantine bucket origin (already in the CSP `connect-src`), never a `<form action>`.
 * The bytes never pass through the API and the web never reads them.
 *
 * Every string a version carries from the upload is untrusted text (types.ts).
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { api, ApiError, unwrap, unwrapVoid } from "@/api/client";
import { qk, useProjectScope, type ProjectScope } from "@/api/queries";
import { currentPath, loginHref } from "@/api/session";
import type { Me } from "@/api/types";

import { pk } from "./keys";
import { CHECKING_STATES } from "./states";
import type {
  Plugin,
  PluginClaim,
  PluginListFilters,
  PluginSettings,
  PluginSettingsPatch,
  PluginUploadTicket,
  PluginUsage,
  PluginVersion,
  PluginVersionSummary,
  PluginYank,
  PlotSpec,
  PostPolicy,
  UploadPhase,
  VerifiedUploader,
  PluginDecision,
} from "./types";
import { UPLOAD_MAX_BYTES } from "./types";

const LIMIT = 200;
const MAX_PAGES = 5;
/** How often a version page polls while the checks run. */
export const CHECKING_POLL_MS = 2000;

// ------------------------------------------------------------------------------ flags (GET /v1/me)

export interface PluginFlags {
  /** The deployment accepts uploads (`plugins_enabled`, default on). */
  enabled: boolean;
  /** Uploaded code runs without a sandbox (dev only: show the banner, D7). */
  devUntrusted: boolean;
  /** The operator paused uploaded code (kill switch): checks wait, "Paused by the platform". */
  paused: boolean;
}

export function pluginFlags(me: Me | null | undefined): PluginFlags {
  return { enabled: me?.plugins_enabled ?? true, devUntrusted: me?.dev_untrusted_code ?? false, paused: me?.plugins_paused ?? false };
}

// ------------------------------------------------------------------------------ problems

/** The approval needs a fresh sign-in (401 `REAUTH_REQUIRED`, D13). */
export function isReauthRequired(err: unknown): boolean {
  return err instanceof ApiError && err.code === "REAUTH_REQUIRED";
}

/**
 * The server refused this reviewer (403: the uploader themselves, or an admin the uploader
 * brought in recently). Another admin must review; a higher role would not help, so the
 * panel shows the server's reason alone, without the generic "ask for a higher role" advice.
 */
export function reviewRefusal(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 403) return null;
  if (err.code !== "FORBIDDEN" && err.code !== "SELF_APPROVAL_FORBIDDEN") return null;
  return err.problem?.detail ?? "You cannot review this version. Ask another tenant admin.";
}

/** The page's digests are stale or the state moved on (409 `PLUGIN_STATE_CONFLICT`): reload. */
export function isStateConflict(err: unknown): boolean {
  return err instanceof ApiError && err.code === "PLUGIN_STATE_CONFLICT";
}

/**
 * Where the reauth round trip goes: sign in, then back to this page. `reauth=1` makes the
 * server ask the identity provider to authenticate the person again (`max_age=0`), so a
 * silent SSO redirect does not count as a fresh sign-in.
 */
export function reauthHref(returnTo: string = currentPath()): string {
  return `${loginHref(returnTo)}&reauth=1`;
}

// ------------------------------------------------------------------------------ plugins

function listQuery(f: PluginListFilters) {
  return { ...(f.state ? { state: f.state } : {}), ...(f.mine ? { mine: true } : {}) };
}

/** Every plugin of the tenant the caller may see that matches `f` (all pages). */
export async function fetchPlugins(s: ProjectScope, f: PluginListFilters = {}, signal?: AbortSignal): Promise<Plugin[]> {
  const out: Plugin[] = [];
  let cursor: string | null | undefined;
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await unwrap(
      api.GET("/v1/plugins", {
        params: { query: { limit: LIMIT, ...listQuery(f), ...(cursor ? { cursor } : {}) } },
        headers: s.headers,
        ...(signal ? { signal } : {}),
      }),
    );
    out.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) break;
  }
  return out;
}

export function usePlugins(f: PluginListFilters = {}, enabled = true) {
  const s = useProjectScope();
  return useQuery({
    queryKey: pk.list(s.key, f),
    queryFn: ({ signal }) => fetchPlugins(s, f, signal),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}

export function usePlugin(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: pk.plugin(s.key, id ?? ""),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/plugins/{plugin_id}", { params: { path: { plugin_id: id ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(id),
  });
}

export function useClaimPlugin() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PluginClaim): Promise<Plugin> => unwrap(api.POST("/v1/plugins", { body, headers: s.headers })),
    onSuccess: (plugin) => {
      qc.setQueryData(pk.plugin(s.key, plugin.id), plugin);
      void qc.invalidateQueries({ queryKey: pk.lists(s.key) });
    },
  });
}

// ------------------------------------------------------------------------------ versions

export function usePluginVersions(id: string | undefined, poll = false) {
  const s = useProjectScope();
  return useQuery({
    queryKey: pk.versions(s.key, id ?? ""),
    queryFn: async ({ signal }): Promise<PluginVersionSummary[]> => {
      const page = await unwrap(
        api.GET("/v1/plugins/{plugin_id}/versions", { params: { path: { plugin_id: id ?? "" }, query: { limit: LIMIT } }, headers: s.headers, signal }),
      );
      return page.items;
    },
    enabled: Boolean(id),
    // Poll while a version is still being checked.
    refetchInterval: (q) => (poll && (q.state.data ?? []).some((v) => CHECKING_STATES.has(v.state)) ? CHECKING_POLL_MS : false),
  });
}

/** Poll delay of a version (`false` once the checks are over). */
export function versionPollDelay(v: Pick<PluginVersion, "state"> | undefined): number | false {
  return v && CHECKING_STATES.has(v.state) ? CHECKING_POLL_MS : false;
}

export function usePluginVersion(id: string | undefined, versionId: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: pk.version(s.key, id ?? "", versionId ?? ""),
    queryFn: ({ signal }): Promise<PluginVersion> =>
      unwrap(
        api.GET("/v1/plugins/{plugin_id}/versions/{version_id}", {
          params: { path: { plugin_id: id ?? "", version_id: versionId ?? "" } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id && versionId),
    refetchInterval: (q) => versionPollDelay(q.state.data),
  });
}

export function usePluginUsage(id: string, versionId: string, enabled = true) {
  const s = useProjectScope();
  return useQuery({
    queryKey: pk.usage(s.key, id, versionId),
    queryFn: ({ signal }): Promise<PluginUsage> =>
      unwrap(api.GET("/v1/plugins/{plugin_id}/versions/{version_id}/usage", { params: { path: { plugin_id: id, version_id: versionId } }, headers: s.headers, signal })),
    enabled: enabled && Boolean(id && versionId),
    staleTime: 0,
  });
}

/** A recipe's example (a sanitized `PlotSpec`; render it in the plot sandbox, never inline). */
export function usePluginExample(id: string, versionId: string, plotId: string | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: pk.example(s.key, id, versionId, plotId ?? ""),
    queryFn: ({ signal }): Promise<PlotSpec> =>
      unwrap(
        api.GET("/v1/plugins/{plugin_id}/versions/{version_id}/examples/{plot_id}", {
          params: { path: { plugin_id: id, version_id: versionId, plot_id: plotId ?? "" } },
          headers: s.headers,
          signal,
        }),
      ) as Promise<PlotSpec>,
    enabled: Boolean(id && versionId && plotId),
    staleTime: Infinity,
  });
}

/** A changed version: store it and refresh the lists and the catalog (the tenant view moves). */
function versionChanged(qc: QueryClient, s: ProjectScope, v: PluginVersion) {
  qc.setQueryData(pk.version(s.key, v.plugin_id, v.id), v);
  void qc.invalidateQueries({ queryKey: pk.versions(s.key, v.plugin_id) });
  void qc.invalidateQueries({ queryKey: pk.plugin(s.key, v.plugin_id), exact: true });
  void qc.invalidateQueries({ queryKey: pk.lists(s.key) });
  void qc.invalidateQueries({ queryKey: qk.catalog });
}

export function useRequestApproval() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ pluginId, versionId }: { pluginId: string; versionId: string }): Promise<PluginVersion> =>
      unwrap(
        api.POST("/v1/plugins/{plugin_id}/versions/{version_id}:request-approval", {
          params: { path: { plugin_id: pluginId, version_id: versionId } },
          headers: s.headers,
        }),
      ),
    onSuccess: (v) => versionChanged(qc, s, v),
  });
}

export interface DecideArgs {
  pluginId: string;
  versionId: string;
  decision: "approve" | "reject";
  /** Build it with `decisionBody` (states.ts): the digests the page showed. */
  body: PluginDecision;
}

/**
 * Approve or reject. 401 `REAUTH_REQUIRED` ⇒ send the user through `reauthHref()`;
 * 409 `PLUGIN_STATE_CONFLICT` ⇒ the version changed: the page reloads it (done here).
 */
export function useDecide() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ pluginId, versionId, decision, body }: DecideArgs): Promise<PluginVersion> => {
      const params = { path: { plugin_id: pluginId, version_id: versionId } };
      return decision === "approve"
        ? unwrap(api.POST("/v1/plugins/{plugin_id}/versions/{version_id}:approve", { params, body, headers: s.headers }))
        : unwrap(api.POST("/v1/plugins/{plugin_id}/versions/{version_id}:reject", { params, body, headers: s.headers }));
    },
    onSuccess: (v) => versionChanged(qc, s, v),
    onError: (err, args) => {
      if (isStateConflict(err)) void qc.invalidateQueries({ queryKey: pk.version(s.key, args.pluginId, args.versionId) });
    },
  });
}

export function useYank() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ pluginId, versionId, body }: { pluginId: string; versionId: string; body: PluginYank }): Promise<PluginVersion> =>
      unwrap(
        api.POST("/v1/plugins/{plugin_id}/versions/{version_id}:yank", {
          params: { path: { plugin_id: pluginId, version_id: versionId } },
          body: { reason: body.reason, ...(body.note?.trim() ? { note: body.note.trim() } : {}) },
          headers: s.headers,
        }),
      ),
    onSuccess: (v) => versionChanged(qc, s, v),
  });
}

// ------------------------------------------------------------------------------ the browser upload

export class WheelUploadError extends Error {
  override name = "WheelUploadError";
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
  }
}

/**
 * POST the wheel to the ticket's POST policy: every policy field, then the file last (the
 * storage service ignores fields after the file). Plain `fetch` with `FormData`; no cookies.
 */
export async function postWheel(post: PostPolicy, file: Blob, filename: string, signal?: AbortSignal): Promise<void> {
  const [min, max] = post.content_length_range;
  if (file.size < (min ?? 1) || file.size > (max ?? UPLOAD_MAX_BYTES)) {
    throw new WheelUploadError(`The file must be between ${min ?? 1} and ${max ?? UPLOAD_MAX_BYTES} bytes.`, null);
  }
  if (new Date(post.expires_at).getTime() <= Date.now()) throw new WheelUploadError("The upload ticket expired. Start the upload again.", null);
  const form = new FormData();
  for (const [k, v] of Object.entries(post.fields ?? {})) form.append(k, v);
  form.append("file", file, filename);
  let res: Response;
  try {
    res = await globalThis.fetch(post.url, { method: "POST", body: form, credentials: "omit", ...(signal ? { signal } : {}) });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new WheelUploadError("The storage service could not be reached.", null);
  }
  if (!res.ok) {
    throw new WheelUploadError(res.status === 412 ? "This upload ticket was already used. Start the upload again." : `The storage service refused the file (HTTP ${res.status}).`, res.status);
  }
}

export interface UploadWheelArgs {
  pluginId: string;
  file: File;
  /** The project that pays for the check runs (defaults to the selected one). */
  project?: string;
  requestApproval?: boolean;
  onPhase?: (phase: UploadPhase) => void;
  signal?: AbortSignal;
}

/** Ticket → POST → `:complete`. Resolves with the new version (state `inspecting`). */
export async function uploadWheel(s: ProjectScope, args: UploadWheelArgs): Promise<PluginVersion> {
  const { pluginId, file } = args;
  args.onPhase?.("ticket");
  const ticket: PluginUploadTicket = await unwrap(
    api.POST("/v1/plugins/{plugin_id}/uploads", {
      params: { path: { plugin_id: pluginId } },
      body: { filename: file.name, size: file.size },
      headers: s.headers,
    }),
  );
  args.onPhase?.("sending");
  await postWheel(ticket.post, file, file.name, args.signal);
  args.onPhase?.("completing");
  const version: PluginVersion = await unwrap(
    api.POST("/v1/plugins/{plugin_id}/uploads/{upload_id}:complete", {
      params: { path: { plugin_id: pluginId, upload_id: ticket.upload_id } },
      body: { project: args.project ?? s.projectId, request_approval: args.requestApproval ?? false },
      headers: s.headers,
    }),
  );
  args.onPhase?.("done");
  return version;
}

export function useUploadWheel() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: UploadWheelArgs) => uploadWheel(s, args),
    onSuccess: (v) => versionChanged(qc, s, v),
  });
}

// ------------------------------------------------------------------------------ tenant settings (admins)

export function usePluginSettings(tenantId: string, enabled = true) {
  return useQuery({
    queryKey: pk.settings(tenantId),
    queryFn: ({ signal }): Promise<PluginSettings> =>
      unwrap(api.GET("/v1/tenants/{tenant_id}/plugin-settings", { params: { path: { tenant_id: tenantId } }, signal })),
    enabled: enabled && Boolean(tenantId),
  });
}

export function useUpdatePluginSettings(tenantId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PluginSettingsPatch): Promise<PluginSettings> =>
      unwrap(api.PATCH("/v1/tenants/{tenant_id}/plugin-settings", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: (settings) => {
      qc.setQueryData(pk.settings(tenantId), settings);
      // Private use changes what everyone's catalog view holds.
      void qc.invalidateQueries({ queryKey: qk.catalog });
      void qc.invalidateQueries({ predicate: (q) => q.queryKey[0] === "p" && q.queryKey[2] === "plugins" });
    },
  });
}

export function usePluginUploaders(tenantId: string, enabled = true) {
  return useQuery({
    queryKey: pk.uploaders(tenantId),
    queryFn: ({ signal }): Promise<VerifiedUploader[]> =>
      unwrap(api.GET("/v1/tenants/{tenant_id}/plugin-uploaders", { params: { path: { tenant_id: tenantId } }, signal })),
    enabled: enabled && Boolean(tenantId),
  });
}

/** Attest (`PUT`, "I know who this person is") or revoke (`DELETE`) an uploader. */
export function useSetUploader(tenantId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ userId, verified }: { userId: string; verified: boolean }): Promise<VerifiedUploader | null> => {
      const params = { path: { tenant_id: tenantId, user_id: userId } };
      if (verified) return unwrap(api.PUT("/v1/tenants/{tenant_id}/plugin-uploaders/{user_id}", { params }));
      await unwrapVoid(api.DELETE("/v1/tenants/{tenant_id}/plugin-uploaders/{user_id}", { params }));
      return null;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: pk.uploaders(tenantId) }),
  });
}
