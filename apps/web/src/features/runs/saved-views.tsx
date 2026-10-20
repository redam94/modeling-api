/**
 * Saved views of the runs table (v0.14): a name and a run query (filters, order, columns,
 * state, kind, name and experiment) stored on the server, shared in the project. Applying a view
 * writes its query to the URL; the menu can save the current table as a new view, save the
 * changes to the applied view, rename it or delete it (the `update` permission on runs).
 * View names are untrusted text.
 */
import { BookmarkPlus, Bookmark, ChevronDown, Pencil, Save, Trash2 } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { ApiError } from "@/api/client";
import { MAX_EXPERIMENT_NAME_CHARS } from "@/api/experiments";
import { useCreateRunView, useDeleteRunView, useRunViews, useUpdateRunView } from "@/api/queries";
import type { RunView } from "@/api/types";
import { ErrorNotice } from "@/components/common";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { Field, Input } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { toast } from "@/stores/toast";

import { sameViewQuery, searchFromViewQuery, viewQueryFromSearch, type RunsSearch } from "./run-search";

/** The message for a refused view name (409: the name is taken). */
function nameError(err: unknown): string | null {
  if (err instanceof ApiError && err.status === 409) return "A view with this name exists. Type a different name.";
  return null;
}

function NameDialog({
  title,
  description,
  initial,
  submit,
  open,
  onOpenChange,
  onSave,
  pending,
  error,
}: {
  title: string;
  description: string;
  initial: string;
  submit: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (name: string) => void;
  pending: boolean;
  error: unknown;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
        {/* mounts on open, so the name starts from `initial` each time */}
        <NameForm initial={initial} submit={submit} onCancel={() => onOpenChange(false)} onSave={onSave} pending={pending} error={error} />
      </DialogContent>
    </Dialog>
  );
}

function NameForm({ initial, submit, onCancel, onSave, pending, error }: { initial: string; submit: string; onCancel: () => void; onSave: (name: string) => void; pending: boolean; error: unknown }) {
  const [name, setName] = useState(initial);
  const taken = nameError(error);
  return (
    <form
      className="grid gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) onSave(name.trim());
      }}
    >
      <Field label="Name" htmlFor="run-view-name" error={taken} hint={`${MAX_EXPERIMENT_NAME_CHARS} characters maximum.`}>
        <Input id="run-view-name" value={name} maxLength={MAX_EXPERIMENT_NAME_CHARS} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      {error && !taken ? <ErrorNotice error={error} title="The app could not save the view" /> : null}
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={!name.trim() || pending}>
          {submit}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function SavedViews({ search, experimentId, onApply }: { search: RunsSearch; experimentId?: string | undefined; onApply: (search: RunsSearch) => void }) {
  const allowed = can(useGrants(), "update", "run");
  const views = useRunViews(experimentId ?? null);
  const create = useCreateRunView();
  const update = useUpdateRunView();
  const remove = useDeleteRunView();
  const [dialog, setDialog] = useState<"save" | "rename" | "delete" | null>(null);
  const items = views.data?.items ?? [];
  const current = items.find((v) => v.id === search.view) ?? null;
  const query = viewQueryFromSearch(search, experimentId);
  const changed = current !== null && !sameViewQuery(current.query, query);
  const apply = (v: RunView) => onApply(searchFromViewQuery(v.query, v.id, experimentId));

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" className="max-w-72">
            <Bookmark />
            <span className="truncate">{current ? `View: ${current.name}` : "Views"}</span>
            {changed ? <span className="text-xs text-muted-foreground">(changed)</span> : null}
            <ChevronDown />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="max-w-80">
          <DropdownMenuLabel>{experimentId ? "Saved views of the experiment" : "Saved views"}</DropdownMenuLabel>
          {views.isError ? <DropdownMenuItem disabled>The app could not load the views</DropdownMenuItem> : null}
          {!views.isError && !items.length ? <DropdownMenuItem disabled>{views.isPending ? "Loading…" : "No saved views"}</DropdownMenuItem> : null}
          {items.map((v) => (
            <DropdownMenuItem key={v.id} onSelect={() => apply(v)} aria-current={v.id === current?.id ? "true" : undefined}>
              <span className="min-w-0 flex-1 truncate" title={v.name}>
                {v.name}
              </span>
              {v.id === current?.id ? <span className="text-xs text-muted-foreground">applied</span> : null}
            </DropdownMenuItem>
          ))}
          {allowed ? (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onSelect={() => {
                  create.reset();
                  setDialog("save");
                }}
              >
                <BookmarkPlus /> Save as a new view
              </DropdownMenuItem>
              {current ? (
                <>
                  <DropdownMenuItem
                    disabled={!changed || update.isPending}
                    onSelect={() =>
                      update.mutate(
                        { id: current.id, patch: { query } },
                        { onSuccess: () => toast.success("The app saved the changes to the view"), onError: (err) => toast.error("The app could not save the view", err.message) },
                      )
                    }
                  >
                    <Save /> Save the changes to the view
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onSelect={() => {
                      update.reset();
                      setDialog("rename");
                    }}
                  >
                    <Pencil /> Rename the view
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onSelect={() => {
                      remove.reset();
                      setDialog("delete");
                    }}
                    className="text-destructive"
                  >
                    <Trash2 /> Delete the view
                  </DropdownMenuItem>
                </>
              ) : null}
            </>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>

      <NameDialog
        title="Save as a new view"
        description="The view keeps the filters, the order and the columns of the table. All the members of the project can apply it."
        initial=""
        submit="Save view"
        open={dialog === "save"}
        onOpenChange={(o) => setDialog(o ? "save" : null)}
        pending={create.isPending}
        error={create.error}
        onSave={(name) =>
          create.mutate(
            { name, experiment_id: experimentId ?? null, query },
            {
              onSuccess: (v) => {
                setDialog(null);
                onApply({ ...search, view: v.id });
                toast.success("The app saved the view");
              },
            },
          )
        }
      />
      {current ? (
        <NameDialog
          title="Rename the view"
          description="Type a new name for the view."
          initial={current.name}
          submit="Rename"
          open={dialog === "rename"}
          onOpenChange={(o) => setDialog(o ? "rename" : null)}
          pending={update.isPending}
          error={update.error}
          onSave={(name) => update.mutate({ id: current.id, patch: { name } }, { onSuccess: () => setDialog(null) })}
        />
      ) : null}
      {current ? (
        <Dialog open={dialog === "delete"} onOpenChange={(o) => setDialog(o ? "delete" : null)}>
          <DialogContent>
            <DialogTitle>Delete the view?</DialogTitle>
            <DialogDescription>
              The app deletes the view <span className="font-medium text-foreground [overflow-wrap:anywhere]">{current.name}</span> for all the members of the project. The runs do not change.
            </DialogDescription>
            {remove.isError ? <ErrorNotice error={remove.error} title="The app could not delete the view" /> : null}
            <DialogFooter>
              <Button variant="ghost" onClick={() => setDialog(null)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={remove.isPending}
                onClick={() =>
                  remove.mutate(current.id, {
                    onSuccess: () => {
                      setDialog(null);
                      onApply({ ...search, view: undefined });
                    },
                  })
                }
              >
                Delete view
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </>
  );
}
