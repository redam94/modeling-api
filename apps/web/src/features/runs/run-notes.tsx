/**
 * The Notes tab of the run page (v0.11): the run's name, tags and note. Save sends a merge
 * patch with only the fields the user changed since the edit started (`PATCH
 * /v1/runs/{id}`); without the `update` permission on runs the tab is read-only. Tags that
 * start with `mapi.` belong to the platform and are shown, never edited.
 *
 * The page can hold the edit (`edit`/`onEdit`) above its tabs, so an unsaved edit survives a
 * switch to another tab (the tab content unmounts).
 */
import { Plus, Save, Trash2, Undo2 } from "lucide-react";
import { useMemo, useState } from "react";

import { can } from "@/api/access";
import { useUpdateRun } from "@/api/queries";
import { MAX_RUN_NAME_CHARS, MAX_TAGS_PER_RUN, runName, runNote, runTags } from "@/api/tracking";
import type { Run } from "@/api/types";
import { ErrorNotice } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, Field, Input, Textarea } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { toast } from "@/stores/toast";

import { TagBadges } from "./tag-badges";
import { draftErrors, draftFromRun, isBlankTagRow, isReservedTag, runPatchDiff, tagRowError, type NotesDraft, type NotesEdit, type TagRow } from "./run-patch";

let rowSeq = 0;

/** May the caller change runs (`run:update`, the runner role and up)? */
function useCanUpdateRun(): boolean {
  return can(useGrants(), "update", "run");
}

function ReadOnlyNotes({ run }: { run: Run }) {
  const grants = useGrants();
  const note = runNote(run);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Notes</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 text-[13px]">
        <p className="text-muted-foreground">
          {grants.projectRole ? `You need the runner role to change the name, tags and note. Your role is ${grants.projectRole}.` : "You cannot change the name, tags and note of this run."}
        </p>
        <div className="grid gap-1">
          <span className="text-xs text-muted-foreground">Name</span>
          <span className="[overflow-wrap:anywhere]">{runName(run) ?? "No name"}</span>
        </div>
        <div className="grid gap-1">
          <span className="text-xs text-muted-foreground">Tags</span>
          {Object.keys(runTags(run)).length ? <TagBadges tags={runTags(run)} /> : <span>No tags</span>}
        </div>
        <div className="grid gap-1">
          <span className="text-xs text-muted-foreground">Note</span>
          {note ? <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{note}</p> : <span>No note</span>}
        </div>
      </CardContent>
    </Card>
  );
}

function TagEditor({ rows, onChange }: { rows: TagRow[]; onChange: (rows: TagRow[]) => void }) {
  const set = (id: string, patch: Partial<TagRow>) => onChange(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  return (
    <div className="grid gap-2" role="group" aria-label="Tags">
      {rows.length ? null : <p className="text-[13px] text-muted-foreground">The run has no tags.</p>}
      {rows.map((r, i) => {
        // a blank row ("Add a tag", not filled) is ignored, so it shows no error
        const error = isBlankTagRow(r) ? "" : tagRowError(r, rows);
        return (
          <div key={r.id} className="grid gap-1">
            <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_auto] gap-2">
              <Input aria-label={`Tag ${i + 1} key`} placeholder="Key" value={r.key} onChange={(e) => set(r.id, { key: e.target.value })} aria-invalid={Boolean(error) || undefined} className="h-8" />
              <Input aria-label={`Tag ${i + 1} value`} placeholder="Value" value={r.value} onChange={(e) => set(r.id, { value: e.target.value })} className="h-8" />
              <Button variant="ghost" size="sm" aria-label={`Remove tag ${i + 1}`} onClick={() => onChange(rows.filter((x) => x.id !== r.id))}>
                <Trash2 />
              </Button>
            </div>
            {error ? <p className="text-xs text-destructive">{error}</p> : null}
          </div>
        );
      })}
      <div>
        <Button variant="outline" size="sm" disabled={rows.length >= MAX_TAGS_PER_RUN} onClick={() => onChange([...rows, { id: `n:${++rowSeq}`, key: "", value: "" }])}>
          <Plus /> Add a tag
        </Button>
      </div>
    </div>
  );
}

/** Does `edit` hold changes to `runId` that are not saved? */
export function hasUnsavedNotes(edit: NotesEdit | null, runId: string): boolean {
  return edit !== null && edit.runId === runId && runPatchDiff(edit.start, edit.draft) !== null;
}

export function RunNotes({ run, edit: held, onEdit }: { run: Run; edit?: NotesEdit | null; onEdit?: (edit: NotesEdit | null) => void }) {
  const allowed = useCanUpdateRun();
  const update = useUpdateRun();
  const base = useMemo(() => draftFromRun(run), [run]);
  const [own, setOwn] = useState<NotesEdit | null>(null);
  const setEdit = onEdit ?? setOwn;
  const current = (onEdit ? held : own) ?? null;
  // Until the user changes something, the form follows the newest run (SSE, another tab).
  const editing = current?.runId === run.id ? current : null;
  const draft = editing?.draft ?? base;
  const start = editing?.start ?? base;

  if (!allowed) return <ReadOnlyNotes run={run} />;

  const edit = (next: NotesDraft) => setEdit({ runId: run.id, start, draft: next });
  const patch = runPatchDiff(start, draft);
  const errors = draftErrors(draft);
  const platformTags = Object.fromEntries(Object.entries(runTags(run)).filter(([k]) => isReservedTag(k)));

  const save = () => {
    if (!patch || errors.length) return;
    update.mutate(
      { id: run.id, patch },
      {
        onSuccess: () => {
          setEdit(null);
          toast.success("The app saved the changes");
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Notes</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="grid max-w-3xl gap-5"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <Field label="Name" htmlFor="run-name" hint={`A short name for the run (${MAX_RUN_NAME_CHARS} characters maximum). The run list shows it.`}>
            <Input id="run-name" value={draft.name} maxLength={MAX_RUN_NAME_CHARS} onChange={(e) => edit({ ...draft, name: e.target.value })} placeholder="No name" />
          </Field>
          <div className="grid gap-1.5">
            <span className="text-[13px] font-medium">Tags</span>
            <TagEditor rows={draft.tags} onChange={(tags) => edit({ ...draft, tags })} />
            {Object.keys(platformTags).length ? (
              <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                The platform set these tags: <TagBadges tags={platformTags} />
              </p>
            ) : null}
          </div>
          <Field label="Note" htmlFor="run-note" hint="Write what you changed in this run and what you found.">
            <Textarea id="run-note" value={draft.note} onChange={(e) => edit({ ...draft, note: e.target.value })} rows={8} />
          </Field>
          {errors.length ? <p className="text-xs text-destructive">{errors.join(" ")}</p> : null}
          {update.isError ? <ErrorNotice error={update.error} title="The app could not save the changes" /> : null}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={!patch || errors.length > 0 || update.isPending}>
              <Save /> Save
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={!patch || update.isPending}
              onClick={() => {
                setEdit(null);
                update.reset();
              }}
            >
              <Undo2 /> Discard the changes
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
