/**
 * Uploaded code (v0.19): the version state machine as the web shows it, type guards per
 * state, who may do what on a version, and pure string helpers for uploaded ids, kit reports,
 * SBOMs and About links.
 *
 * The transitions mirror `mapi_spec.plugins.PLUGIN_TRANSITIONS` (D8 + the D12 rekit edge);
 * the web never invents a state change, it only decides which buttons to offer. The server
 * re-checks every action. Labels are the relaxed voice of the upload pages (v0.18 D28);
 * `INSPECT_CHECK_LABEL` is reference text (STE).
 *
 * Nothing here touches the network or the DOM.
 */
import type { CatalogResponse, Me } from "@/api/types";
import type { Tone } from "@/lib/run-state";

import type {
  AnyPluginVersion,
  ExportKind,
  InspectCheck,
  KitCheck,
  KitStatus,
  PluginDecision,
  PluginKitReport,
  PluginSbom,
  PluginVersion,
  PluginVersionState,
  RequirementFact,
  UploadedCatalogEntry,
  UploadNamespace,
} from "./types";
import { EXPORT_KINDS, KIT_STATUSES, PLUGIN_VERSION_STATES } from "./types";

// ------------------------------------------------------------------------------ states

/** The state chip's words (I-8). */
export const STATE_LABEL: Record<PluginVersionState, string> = {
  uploaded: "Received",
  inspecting: "Checking the files",
  inspect_failed: "Refused",
  describing: "Running the tests",
  kit_failed: "Tests failed",
  kit_passed: "Ready for you",
  awaiting_approval: "Waiting for a reviewer",
  approved: "Approved for the team",
  rejected: "Not approved",
  yanked: "Withdrawn",
  purged: "Removed",
};

/** The chip's tone (the run-state palette: coral `danger` for refusals). */
export const STATE_TONE: Record<PluginVersionState, Tone> = {
  uploaded: "active",
  inspecting: "active",
  inspect_failed: "danger",
  describing: "active",
  kit_failed: "danger",
  kit_passed: "success",
  awaiting_approval: "warning",
  approved: "success",
  rejected: "danger",
  yanked: "muted",
  purged: "muted",
};

/** One friendly sentence per state (the version page under the chip). */
export const STATE_DESCRIPTION: Record<PluginVersionState, string> = {
  uploaded: "We have your file and start checking it in a moment.",
  inspecting: "We're reading the wheel without running any of it: file names, sizes, entry points and requirements.",
  inspect_failed: "We found problems in the file, so none of it ran. Fix them and upload a new version.",
  describing: "Your code is running in a sandbox so we can see what it adds and run the tests.",
  kit_failed: "Some tests failed. The report below shows which ones and why.",
  kit_passed: "Every test passed. You can use it yourself now, and ask a reviewer to approve it for the team.",
  awaiting_approval: "A workspace admin other than the uploader looks at it next.",
  approved: "Everyone in the workspace can use it.",
  rejected: "A reviewer didn't approve this version. Their note says why.",
  yanked: "This version was withdrawn. New runs can't use it.",
  purged: "The file was removed after 30 days. This record stays.",
};

/** Shown instead of the state's sentence while the platform holds uploaded-code runs (kill switch). */
export const PAUSED_LABEL = "Paused by the platform";
export const PAUSED_DESCRIPTION = "Runs of uploaded code are paused for now. Checks continue when they start again.";

/** The label on private cards and versions (D20). */
export const ONLY_YOU_LABEL = "Only you can use this";

/** `mapi_spec.plugins.PLUGIN_TRANSITIONS`. */
export const PLUGIN_TRANSITIONS: Record<PluginVersionState, readonly PluginVersionState[]> = {
  uploaded: ["inspecting"],
  inspecting: ["inspect_failed", "describing"],
  inspect_failed: ["purged"],
  describing: ["kit_failed", "kit_passed"],
  kit_failed: ["purged"],
  kit_passed: ["awaiting_approval", "yanked"],
  awaiting_approval: ["approved", "rejected", "yanked"],
  approved: ["yanked", "awaiting_approval"],
  rejected: ["purged"],
  yanked: ["purged"],
  purged: [],
};

export function canTransition(from: PluginVersionState, to: PluginVersionState): boolean {
  return PLUGIN_TRANSITIONS[from].includes(to);
}

export function isPluginVersionState(v: unknown): v is PluginVersionState {
  return typeof v === "string" && (PLUGIN_VERSION_STATES as readonly string[]).includes(v);
}

/** `mapi_spec.plugins.MOUNTABLE_STATES`: a run may use the version (if the caller may see it). */
export const MOUNTABLE_STATES: ReadonlySet<PluginVersionState> = new Set(["kit_passed", "awaiting_approval", "approved"]);
/** The checks are still going: poll the version. */
export const CHECKING_STATES: ReadonlySet<PluginVersionState> = new Set(["uploaded", "inspecting", "describing"]);
/** Nothing will happen to the version except the 30-day purge. */
export const FINAL_STATES: ReadonlySet<PluginVersionState> = new Set(["inspect_failed", "kit_failed", "rejected", "yanked", "purged"]);

// ------------------------------------------------------------------------------ type guards

type InState<V extends AnyPluginVersion, St extends PluginVersionState> = V & { state: St };

/** `v` is in state `s` (narrows `state`). */
export function isState<V extends AnyPluginVersion, St extends PluginVersionState>(v: V, s: St): v is InState<V, St> {
  return v.state === s;
}

/** Runs may mount it (kit passed, waiting for a reviewer or approved). */
export function isMountable<V extends AnyPluginVersion>(v: V): v is InState<V, "kit_passed" | "awaiting_approval" | "approved"> {
  return MOUNTABLE_STATES.has(v.state);
}

/** The pipeline still works on it (received, checking the files or running the tests). */
export function isChecking<V extends AnyPluginVersion>(v: V): v is InState<V, "uploaded" | "inspecting" | "describing"> {
  return CHECKING_STATES.has(v.state);
}

/** The inspect run refused the file (nothing from it ran). */
export function isRefused<V extends AnyPluginVersion>(v: V): v is InState<V, "inspect_failed"> {
  return v.state === "inspect_failed";
}

export function isKitFailed<V extends AnyPluginVersion>(v: V): v is InState<V, "kit_failed"> {
  return v.state === "kit_failed";
}

/** Tests passed: usable by its uploader (private use), not yet sent for review. */
export function isKitPassed<V extends AnyPluginVersion>(v: V): v is InState<V, "kit_passed"> {
  return v.state === "kit_passed";
}

export function isAwaitingApproval<V extends AnyPluginVersion>(v: V): v is InState<V, "awaiting_approval"> {
  return v.state === "awaiting_approval";
}

export function isApproved<V extends AnyPluginVersion>(v: V): v is InState<V, "approved"> {
  return v.state === "approved";
}

export function isRejected<V extends AnyPluginVersion>(v: V): v is InState<V, "rejected"> {
  return v.state === "rejected";
}

export function isYanked<V extends AnyPluginVersion>(v: V): v is InState<V, "yanked"> {
  return v.state === "yanked";
}

export function isPurged<V extends AnyPluginVersion>(v: V): v is InState<V, "purged"> {
  return v.state === "purged";
}

/** Refused, tests failed or not approved (something to fix in a new version). */
export function isFailed<V extends AnyPluginVersion>(v: V): v is InState<V, "inspect_failed" | "kit_failed" | "rejected"> {
  return v.state === "inspect_failed" || v.state === "kit_failed" || v.state === "rejected";
}

/** Only its uploader can use it (scope `private`). */
export function isPrivate<V extends AnyPluginVersion>(v: V): v is V & { scope: "private" } {
  return v.scope === "private";
}

/** A version a reviewer can decide on: waiting, with the three digests the decision repeats. */
export type DecidableVersion = PluginVersion & {
  state: "awaiting_approval";
  overlay_sha256: string;
  describe_digest: string;
  base_image_digest: string;
};

export function isDecidable(v: PluginVersion): v is DecidableVersion {
  return v.state === "awaiting_approval" && Boolean(v.overlay_sha256) && Boolean(v.describe_digest) && Boolean(v.base_image_digest);
}

/** The body of `:approve` / `:reject`: the digests the page showed (stale ⇒ 409 `PLUGIN_STATE_CONFLICT`). */
export function decisionBody(v: DecidableVersion, note?: string | null): PluginDecision {
  const trimmed = note?.trim() ?? "";
  return {
    overlay_sha256: v.overlay_sha256,
    describe_digest: v.describe_digest,
    base_image_digest: v.base_image_digest,
    ...(trimmed ? { note: trimmed } : {}),
  };
}

// ------------------------------------------------------------------------------ who may act

/** The signed-in user's id (`null` for a token or service caller). */
export function meUserId(me: Me | null | undefined): string | null {
  return me?.principal.kind === "user" ? me.principal.id : null;
}

/** A browser session (API tokens never approve, reject, yank or attest: D13). */
export function isSession(me: Me | null | undefined): boolean {
  return me?.auth === "session";
}

export function isTenantAdmin(me: Me | null | undefined, tenantId: string): boolean {
  return (me?.tenants ?? []).some((t) => t.tenant.id === tenantId && t.role === "admin");
}

export function isUploader(v: AnyPluginVersion, me: Me | null | undefined): boolean {
  const id = meUserId(me);
  return id !== null && v.uploaded_by === id;
}

/**
 * Why the approve and reject buttons are off (`null`: they are on). Checked in the server's
 * order: the state, the digests, a session, a tenant admin, not the uploader.
 */
export function decisionBlockReason(v: PluginVersion, me: Me | null | undefined, tenantId: string): string | null {
  if (v.state !== "awaiting_approval") return "Only a version that is waiting for a reviewer can be approved or rejected.";
  if (!isDecidable(v)) return "This version has no test results to review yet.";
  if (!isSession(me)) return "Sign in with your browser to review. API tokens can't approve uploaded code.";
  if (!isTenantAdmin(me, tenantId)) return "Only workspace admins can approve or reject uploaded code.";
  if (isUploader(v, me)) return "You uploaded this version, so another admin needs to review it.";
  return null;
}

/** The uploader (or a tenant admin) may send a version that passed its tests for review. */
export function canRequestApproval(v: AnyPluginVersion, me: Me | null | undefined, tenantId: string): boolean {
  return v.state === "kit_passed" && (isUploader(v, me) || isTenantAdmin(me, tenantId));
}

/** The uploader (own versions) or a tenant admin may withdraw a usable version, in a session. */
export function canYank(v: AnyPluginVersion, me: Me | null | undefined, tenantId: string): boolean {
  return isMountable(v) && isSession(me) && (isUploader(v, me) || isTenantAdmin(me, tenantId));
}

/** Only one admin in the workspace: nobody can approve, uploads stay private (D13). */
export function singleAdminNote(adminCount: number): string | null {
  return adminCount < 2 ? "Your workspace has one admin, so uploads can only be used privately by the person who uploaded them." : null;
}

// ------------------------------------------------------------------------------ exports, kit, SBOM

/** Exports per kind (missing kinds are 0). */
export function exportCounts(v: AnyPluginVersion): Record<ExportKind, number> {
  const counts = v.exports ?? {};
  return Object.fromEntries(EXPORT_KINDS.map((k) => [k, counts[k] ?? 0])) as Record<ExportKind, number>;
}

export function exportTotal(v: AnyPluginVersion): number {
  return Object.values(exportCounts(v)).reduce((a, b) => a + b, 0);
}

export const EXPORT_LABEL: Record<ExportKind, [singular: string, plural: string]> = {
  model: ["model", "models"],
  step: ["preparation step", "preparation steps"],
  plot: ["chart", "charts"],
  node: ["workflow step", "workflow steps"],
  template: ["workflow template", "workflow templates"],
};

/** "2 models, 1 chart" (kinds with none are left out; "nothing yet" when empty). */
export function exportsSummary(v: AnyPluginVersion): string {
  const counts = exportCounts(v);
  const parts = EXPORT_KINDS.filter((k) => counts[k] > 0).map((k) => `${counts[k]} ${EXPORT_LABEL[k][counts[k] === 1 ? 0 : 1]}`);
  return parts.length ? parts.join(", ") : "nothing yet";
}

export const KIT_STATUS_LABEL: Record<KitStatus, string> = { pass: "Passed", fail: "Failed", skipped: "Skipped" };

/** Kit checks grouped by status, failures first, each group in report order. */
export function groupKit(report: PluginKitReport | null | undefined): { status: KitStatus; checks: KitCheck[] }[] {
  const checks = report?.checks ?? [];
  return KIT_STATUSES.map((status) => ({ status, checks: checks.filter((c) => c.status === status) })).filter((g) => g.checks.length > 0);
}

export function kitCounts(report: PluginKitReport | null | undefined): Record<KitStatus, number> {
  const out: Record<KitStatus, number> = { pass: 0, fail: 0, skipped: 0 };
  for (const c of report?.checks ?? []) out[c.status] += 1;
  return out;
}

export interface SbomSummary {
  files: number;
  bytes: number;
  requirements: RequirementFact[];
  /** Requirements the base image does not satisfy (`locked_version` missing). */
  unlocked: number;
  license: string | null;
}

export function sbomSummary(sbom: PluginSbom | null | undefined): SbomSummary | null {
  if (!sbom) return null;
  const files = sbom.files ?? [];
  const requirements = sbom.requires ?? [];
  return {
    files: files.length,
    bytes: files.reduce((a, f) => a + f.size, 0),
    requirements,
    unlocked: requirements.filter((r) => !r.locked_version).length,
    license: sbom.license ?? null,
  };
}

/** Plain names of the inspect checks (reference text: STE). */
export const INSPECT_CHECK_LABEL: Record<InspectCheck, string> = {
  archive_size: "File is too large",
  member_count: "Too many files",
  member_size: "A file is too large",
  ratio: "Compression ratio is too high",
  path_unsafe: "Unsafe file path",
  link_member: "Link in the archive",
  special_member: "Special file in the archive",
  duplicate_name: "Same file name two times",
  case_collision: "File names differ only in case",
  zip_structure: "Archive structure is not valid",
  encrypted: "Encrypted file",
  compression_method: "Compression method is not supported",
  nested_archive: "Archive inside the archive",
  native_code: "Compiled code",
  bytecode: "Python bytecode",
  pth_file: "Path configuration file (.pth)",
  site_hook: "Start-up hook",
  top_level_name: "Package name is not correct",
  namespace_package: "Package has no __init__.py",
  dist_name: "Distribution name is not correct",
  dist_info: "Package metadata is not valid",
  wheel_tag: "Wheel is not pure Python",
  data_dir: "Data or script directory",
  entry_point_group: "Entry point group is not allowed",
  entry_point_target: "Entry point target is not allowed",
  requirement_unlocked: "Requirement is not in the base image",
  requirement_url: "Requirement points to a URL",
  requires_python: "Python version does not match",
  metadata_invalid: "Metadata is not valid",
  text_unsafe: "Hidden characters in text",
  sha_mismatch: "File changed after upload",
};

// ------------------------------------------------------------------------------ namespaces and ids

const CODE_CHARS = "abcdefghijklmnopqrstuvwxyz234567";
const LOWER = "abcdefghijklmnopqrstuvwxyz";
const DIGITS = "0123456789";

function allIn(s: string, chars: string): boolean {
  for (const ch of s) if (!chars.includes(ch)) return false;
  return true;
}

/** `mapi_spec.plugins.parse_upload_namespace`: `t_<code>_<short>` → its parts, else `null`. */
export function parseUploadNamespace(ns: string | null | undefined): UploadNamespace | null {
  if (!ns) return null;
  const parts = ns.split("_");
  if (parts.length !== 3 || parts[0] !== "t") return null;
  const code = parts[1] ?? "";
  const short = parts[2] ?? "";
  if (code.length !== 6 || !allIn(code, CODE_CHARS)) return null;
  if (short.length < 2 || short.length > 16 || !LOWER.includes(short[0] ?? "") || !allIn(short, LOWER + DIGITS)) return null;
  return { code, short };
}

export function isUploadedNamespace(ns: string | null | undefined): boolean {
  return parseUploadNamespace(ns) !== null;
}

/** `t_<code>_<short>` of `t_<code>_<short>_<rest>` (node names, template ids). */
function prefixedNamespace(name: string): string | null {
  const parts = name.split("_");
  if (parts.length < 4) return null;
  const rest = parts.slice(3).join("_");
  if (!rest || !allIn(rest, `${LOWER}${DIGITS}_`)) return null;
  const ns = parts.slice(0, 3).join("_");
  return isUploadedNamespace(ns) ? ns : null;
}

/**
 * `mapi_spec.namespaces.uploaded_namespace_of`: the uploaded namespace of a catalog id or ref
 * (models/steps/plots `t_k3vq7m_acme.x`, nodes `<kind>.t_k3vq7m_acme_x`, templates
 * `t_k3vq7m_acme_x`; a `@range` is ignored), else `null`.
 */
export function uploadedNamespaceOf(id: string): string | null {
  const bare = id.split("@")[0] ?? "";
  const dot = bare.indexOf(".");
  if (dot >= 0) {
    const head = bare.slice(0, dot);
    const rest = bare.slice(dot + 1);
    if (isUploadedNamespace(head)) return rest ? head : null;
    if (head.length > 0 && allIn(head, LOWER) && !rest.includes(".")) return prefixedNamespace(rest);
    return null;
  }
  return prefixedNamespace(bare);
}

export function isUploadedId(id: string): boolean {
  return uploadedNamespaceOf(id) !== null;
}

/** The distribution name an upload must have (`t-k3vq7m-acme`). */
export function uploadDistName(namespace: string): string {
  return namespace.split("_").join("-");
}

/** The plugin version id of a catalog entry's `provider` (`upload:<pvid>`), else `null`. */
export function uploadVersionIdOf(provider: string | null | undefined): string | null {
  if (!provider || !provider.startsWith("upload:")) return null;
  const id = provider.slice("upload:".length);
  return id.startsWith("plv_") && id.length > 4 ? id : null;
}

/** The uploaded entries of a (tenant view) catalog response, in catalog order. */
export function uploadedCatalogEntries(catalog: CatalogResponse | null | undefined): UploadedCatalogEntry[] {
  if (!catalog) return [];
  const out: UploadedCatalogEntry[] = [];
  const add = (kind: UploadedCatalogEntry["kind"], id: string, version: string, title: string, provider: string | null | undefined) => {
    const versionId = uploadVersionIdOf(provider);
    const namespace = uploadedNamespaceOf(id);
    if (versionId && namespace) out.push({ kind, id, version, title, namespace, versionId });
  };
  for (const m of catalog.models ?? []) add("model", m.manifest.id, m.manifest.version, m.manifest.about?.title ?? m.manifest.id, m.provider);
  for (const s of catalog.steps ?? []) add("step", s.id, s.version, s.about?.title ?? s.id, s.provider);
  for (const p of catalog.plots ?? []) add("plot", p.id, p.version ?? "", p.title, p.provider);
  return out;
}

/** The CLI command the upload page copies (`mapi plugin push`, D21). */
export function pushCommand(short: string, project?: string): string {
  return `mapi plugin push --plugin ${short}${project ? ` --project ${project}` : ""} --wait`;
}

/** The CLI command that scaffolds an upload-ready package (`mapi plugin init --upload`). */
export function initUploadCommand(short: string): string {
  return `mapi plugin init ${short} --upload`;
}

// ------------------------------------------------------------------------------ untrusted text

const BIDI = new Set([0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069, 0x200e, 0x200f, 0x061c]);
const ZERO_WIDTH = new Set([0x200b, 0x200c, 0x200d, 0x2060, 0xfeff, 0x180e, 0x00ad]);

function dropped(code: number): boolean {
  if (code === 0x0a || code === 0x09) return false;
  if (BIDI.has(code) || ZERO_WIDTH.has(code)) return true;
  return code < 0x20 || (code >= 0x7f && code <= 0x9f);
}

/** `mapi_spec.text_safety.has_unsafe_text`: bidi, zero-width or control characters, or not NFC. */
export function hasUnsafeText(s: string): boolean {
  for (const ch of s) if (dropped(ch.codePointAt(0) ?? 0)) return true;
  return s.normalize("NFC") !== s;
}

/** A display copy without bidi, zero-width and control characters (the server already did this). */
export function displayText(s: string): string {
  let out = "";
  for (const ch of s.normalize("NFC")) if (!dropped(ch.codePointAt(0) ?? 0)) out += ch;
  return out;
}

/** `mapi_spec.text_safety.REFERENCE_URL_MAX`. */
export const REFERENCE_URL_MAX = 500;

export type ReferenceLink = { ok: true; href: string; host: string } | { ok: false; reason: string };

function ipLiteral(host: string): boolean {
  if (host.startsWith("[") || host.includes(":")) return true;
  const parts = host.split(".");
  return parts.length === 4 && parts.every((p) => p.length > 0 && allIn(p, DIGITS));
}

function hostLabelOk(label: string): boolean {
  return label.length >= 1 && label.length <= 63 && allIn(label, `${LOWER}${DIGITS}-`) && !label.startsWith("-") && !label.endsWith("-");
}

/**
 * `mapi_spec.text_safety.check_reference_url` on the web: may `url` (from an uploaded About
 * card) be a link? Only `https://` with an ASCII DNS host (punycode `xn--` is fine), no
 * `user@`, no IP address, no spaces or hidden characters. `host` is shown next to the link.
 * Anything else renders as plain text (a `javascript:` URL is never a link).
 */
export function referenceLink(url: string): ReferenceLink {
  if (url.length > REFERENCE_URL_MAX) return { ok: false, reason: `The link is longer than ${REFERENCE_URL_MAX} characters.` };
  for (const ch of url) {
    const code = ch.codePointAt(0) ?? 0;
    if (dropped(code) || code === 0x20 || ch.trim() === "") return { ok: false, reason: "The link contains spaces or hidden characters." };
  }
  if (url.normalize("NFC") !== url) return { ok: false, reason: "The link contains hidden characters." };
  if (url.slice(0, 8).toLowerCase() !== "https://") return { ok: false, reason: "Links must start with https://." };
  const rest = url.slice(8);
  let end = rest.length;
  for (const sep of ["/", "?", "#", "\\"]) {
    const i = rest.indexOf(sep);
    if (i >= 0 && i < end) end = i;
  }
  const authority = rest.slice(0, end);
  if (authority.includes("@")) return { ok: false, reason: "Links must not contain a user name or password before the host." };
  let host = authority;
  const colon = host.lastIndexOf(":");
  if (colon >= 0 && !host.startsWith("[")) {
    const port = host.slice(colon + 1);
    if (!allIn(port, DIGITS)) return { ok: false, reason: "The link is not a valid URL." };
    host = host.slice(0, colon);
  }
  host = host.toLowerCase();
  if (host.endsWith(".")) host = host.slice(0, -1);
  if (!host) return { ok: false, reason: "The link has no host name." };
  for (const ch of host) {
    if ((ch.codePointAt(0) ?? 0) > 0x7f) return { ok: false, reason: "Write the host name in ASCII (punycode, xn--), not in other scripts." };
  }
  if (ipLiteral(host)) return { ok: false, reason: "Links must use a host name, not an IP address." };
  const labels = host.split(".");
  if (labels.length < 2 || !labels.every(hostLabelOk)) return { ok: false, reason: "The link's host name is not a valid DNS name." };
  let href: string;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) return { ok: false, reason: "The link is not a valid URL." };
    href = parsed.href;
  } catch {
    return { ok: false, reason: "The link is not a valid URL." };
  }
  return { ok: true, href, host };
}

/** `rel` of every link from an uploaded About card (D19). */
export const UPLOADED_LINK_REL = "noopener noreferrer nofollow ugc";

// ------------------------------------------------------------------------------ files

/** `{dist}-{version}-py3-none-any.whl` with the filename rule of `PluginUploadTicketRequest`. */
export function wheelFileProblem(name: string, size: number, maxBytes: number): string | null {
  if (!name.toLowerCase().endsWith(".whl")) {
    return name.endsWith(".tar.gz") || name.endsWith(".zip") ? "Upload a wheel. `mapi plugin push` builds one for you." : "Choose a .whl file.";
  }
  if (name.length > 255 || !allIn(name, `${LOWER}${LOWER.toUpperCase()}${DIGITS}._-`)) return "The file name can only use letters, digits, dots, dashes and underscores.";
  if (!name.endsWith("-py3-none-any.whl")) return "Only pure-Python wheels (py3-none-any) can be uploaded.";
  if (size <= 0) return "The file is empty.";
  if (size > maxBytes) return `The file is larger than ${Math.round(maxBytes / 1_048_576)} MiB.`;
  return null;
}

/** `{dist}` and `{version}` of a wheel file name, else `null`. */
export function parseWheelName(name: string): { dist: string; version: string } | null {
  const parts = name.split("-");
  if (parts.length !== 5 || !name.endsWith("-py3-none-any.whl")) return null;
  const [dist, version] = parts;
  return dist && version ? { dist, version } : null;
}
