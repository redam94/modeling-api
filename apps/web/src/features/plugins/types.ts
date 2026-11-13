/**
 * Uploaded code (v0.19, I-1/I-5): the DTOs of `/v1/plugins…` and the tenant plugin settings,
 * generated from the OpenAPI document, plus the small web-only shapes the pages share.
 *
 * Every string a version carries that came from the upload (titles, About text, kit messages,
 * SBOM paths, entry point names, reviewer notes, URLs) is untrusted text: render it as React
 * text only, never as markup, and never as a link unless `referenceLink` (states.ts) allows it.
 *
 * This module holds types and constant tables only.
 */
import type { components } from "@/api/schema";
import type { Issue, PlotSpec, Problem } from "@/api/types";

/** openapi-fetch hands back `Readable<T>` data (tuples become arrays): the same mapping here. */
type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

// ------------------------------------------------------------------------------ DTOs (I-1, I-5)

export type Plugin = S["Plugin"];
export type PluginVersionSummary = S["PluginVersionSummary"];
export type PluginVersion = S["PluginVersion"];
export type PluginDecisionRecord = S["PluginDecisionRecord"];
export type PluginClaim = S["PluginClaim"];
export type PluginUploadTicketRequest = S["PluginUploadTicketRequest"];
export type PluginUploadTicket = S["PluginUploadTicket"];
export type PluginUploadComplete = S["PluginUploadComplete"];
export type PluginDecision = S["PluginDecision"];
export type PluginYank = S["PluginYank"];
export type PluginUsage = S["PluginUsage"];
export type PluginSettings = S["PluginSettings"];
export type PluginSettingsPatch = S["PluginSettingsPatch"];
export type VerifiedUploader = S["VerifiedUploader"];
export type PluginInspection = S["PluginInspection"];
export type PluginSbom = S["PluginSbom"];
export type SbomFile = S["SbomFile"];
export type RequirementFact = S["RequirementFact"];
export type EntryPointDecl = S["EntryPointDecl"];
export type InspectProblem = S["InspectProblem"];
export type PluginKitReport = S["PluginKitReport"];
export type KitCheck = S["KitCheck"];
export type PluginExports = S["PluginExports"];
export type PostPolicy = S["PostPolicy"];
export type OverlayPin = S["OverlayPin"];
export type { Issue, PlotSpec, Problem };

export type PluginVersionState = PluginVersionSummary["state"];
export type PluginScope = PluginVersionSummary["scope"];
export type InspectCheck = InspectProblem["check"];
export type KitStatus = KitCheck["status"];
export type KitKind = KitCheck["kind"];
export type YankReason = NonNullable<PluginVersion["yank_reason"]>;
/** The reasons a person may give (`rekit_failed` is the platform's). */
export type YankChoice = PluginYank["reason"];
export type DecisionKind = PluginDecisionRecord["decision"];
/** Keys of `PluginVersionSummary.exports` (counts per kind). */
export type ExportKind = "model" | "step" | "plot" | "node" | "template";

/** Either version shape (lists carry the summary, the version page the full record). */
export type AnyPluginVersion = PluginVersionSummary | PluginVersion;

// ------------------------------------------------------------------------------ constants

/** Every state, in the order of the pipeline (D8). */
export const PLUGIN_VERSION_STATES = [
  "uploaded",
  "inspecting",
  "inspect_failed",
  "describing",
  "kit_failed",
  "kit_passed",
  "awaiting_approval",
  "approved",
  "rejected",
  "yanked",
  "purged",
] as const satisfies readonly PluginVersionState[];

export const EXPORT_KINDS = ["model", "step", "plot", "node", "template"] as const satisfies readonly ExportKind[];

export const YANK_CHOICES = ["withdrawn", "security", "superseded"] as const satisfies readonly YankChoice[];

export const KIT_STATUSES = ["fail", "pass", "skipped"] as const satisfies readonly KitStatus[];

/** `mapi_spec.plugins` caps the web checks before it asks for a ticket (the server re-checks). */
export const UPLOAD_MAX_BYTES = 20_971_520;
/** Reviewer and yank notes (`PluginDecision.note`, `PluginYank.note`). */
export const NOTE_MAX = 2000;
/** `PluginClaim.title`. */
export const TITLE_MAX = 120;
/** `PLUGIN_SHORT_RE` length bounds (`^[a-z][a-z0-9]{1,15}$`). */
export const SHORT_NAME_MIN = 2;
export const SHORT_NAME_MAX = 16;

// ------------------------------------------------------------------------------ web-only shapes

/** Filters of `GET /v1/plugins`. */
export interface PluginListFilters {
  state?: PluginVersionState;
  mine?: boolean;
}

/** Where a browser upload is (the upload page's progress line). */
export type UploadPhase = "ticket" | "sending" | "completing" | "done";

/** The `{tenant code, short name}` of an uploaded namespace (`t_<code>_<short>`). */
export interface UploadNamespace {
  code: string;
  short: string;
}

/** An uploaded catalog entry as the "Uploaded" section lists it (from the tenant catalog view). */
export interface UploadedCatalogEntry {
  kind: "model" | "step" | "plot";
  id: string;
  version: string;
  /** Untrusted text. */
  title: string;
  namespace: string;
  /** `upload:<plugin version id>` → the version id. */
  versionId: string;
}
