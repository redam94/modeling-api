/**
 * The Notes tab's form model: the editable part of a run (name, note, tags) and the merge
 * patch (`PATCH /v1/runs/{id}`) that changes only what the user changed.
 *
 * The patch compares the form with the run as it was when the user started to edit
 * (`NotesEdit.start`), not with the newest cached run: a tag that the runner sets, or a name
 * that another user changes while this user edits, is never removed or reverted on save.
 */
import {
  MAX_RUN_NAME_CHARS,
  MAX_RUN_NOTE_CHARS,
  MAX_TAG_VALUE_CHARS,
  MAX_TAGS_PER_RUN,
  RESERVED_TAG_PREFIX,
  runName,
  runNote,
  runTags,
  TAG_KEY_RE,
  type RunPatch,
} from "@/api/tracking";
import type { Run } from "@/api/types";

export interface TagRow {
  /** Stable React key (rows can be renamed). */
  id: string;
  key: string;
  value: string;
}

export interface NotesDraft {
  name: string;
  note: string;
  tags: TagRow[];
}

/** An edit in progress: the form as it was when the user started (`start`) and now. */
export interface NotesEdit {
  runId: string;
  start: NotesDraft;
  draft: NotesDraft;
}

export function isReservedTag(key: string): boolean {
  return key.startsWith(RESERVED_TAG_PREFIX);
}

/** The form state of a run. Platform tags (`mapi.*`) are not editable and stay out of it. */
export function draftFromRun(run: Run): NotesDraft {
  return {
    name: runName(run) ?? "",
    note: runNote(run) ?? "",
    tags: Object.entries(runTags(run))
      .filter(([k]) => !isReservedTag(k))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, value]) => ({ id: `t:${key}`, key, value })),
  };
}

/** A row with no key and no value: "Add a tag" that the user did not fill. It is ignored. */
export function isBlankTagRow(row: TagRow): boolean {
  return !row.key.trim() && !row.value.trim();
}

/** Problems of a tag row, in STE ("" when the row is valid or blank). */
export function tagRowError(row: TagRow, rows: readonly TagRow[]): string {
  if (isBlankTagRow(row)) return "";
  const key = row.key.trim();
  if (!key) return "Type a key.";
  if (isReservedTag(key)) return `Keys that start with "${RESERVED_TAG_PREFIX}" are for the platform.`;
  if (!TAG_KEY_RE.test(key)) return "Use letters, digits and the characters _ . - / (128 characters maximum).";
  if (rows.filter((r) => r.key.trim() === key).length > 1) return "Each key can occur only one time.";
  if (row.value.length > MAX_TAG_VALUE_CHARS) return `Use ${MAX_TAG_VALUE_CHARS} characters or fewer.`;
  return "";
}

export function draftErrors(draft: NotesDraft): string[] {
  const errors: string[] = [];
  if (draft.name.trim().length > MAX_RUN_NAME_CHARS) errors.push(`The name has more than ${MAX_RUN_NAME_CHARS} characters.`);
  if (draft.note.length > MAX_RUN_NOTE_CHARS) errors.push(`The note has more than ${MAX_RUN_NOTE_CHARS.toLocaleString("en")} characters.`);
  if (draft.tags.filter((r) => !isBlankTagRow(r)).length > MAX_TAGS_PER_RUN) errors.push(`A run can have ${MAX_TAGS_PER_RUN} tags maximum.`);
  if (draft.tags.some((r) => tagRowError(r, draft.tags))) errors.push("Correct the tags.");
  return errors;
}

const formName = (d: NotesDraft) => d.name.trim() || null;
const formNote = (d: NotesDraft) => (d.note.trim() ? d.note : null);
const formTags = (d: NotesDraft) => new Map(d.tags.filter((r) => !isBlankTagRow(r)).map((r) => [r.key.trim(), r.value]));

/**
 * The merge patch from `start` (the form when the user started to edit, see
 * {@link draftFromRun}) to `draft`: only the fields and tags the user changed. A cleared
 * name or note is `null`; a removed tag is `null`; blank tag rows are ignored; `null` when
 * nothing changed.
 */
export function runPatchDiff(start: NotesDraft, draft: NotesDraft): RunPatch | null {
  const patch: RunPatch = {};
  const name = formName(draft);
  if (name !== formName(start)) patch.name = name;
  const note = formNote(draft);
  if (note !== formNote(start)) patch.note = note;
  const before = formTags(start);
  const after = formTags(draft);
  const tags: Record<string, string | null> = {};
  for (const [k, v] of after) if (before.get(k) !== v) tags[k] = v;
  for (const k of before.keys()) if (!isReservedTag(k) && !after.has(k)) tags[k] = null;
  if (Object.keys(tags).length) patch.tags = tags;
  return Object.keys(patch).length ? patch : null;
}
