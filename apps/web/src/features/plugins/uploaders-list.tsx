/**
 * Verified uploaders (D2): the editors a workspace admin has confirmed ("I know who this
 * person is"), who may then upload code; admins may upload without it and cannot confirm
 * themselves. Attest and revoke need a browser session (the server refuses tokens).
 */
import { ShieldCheck, UserMinus } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { errorMessage } from "@/api/client";
import { useMembers } from "@/api/queries";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Field, NativeSelect } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { relativeTime, shortId } from "@/lib/format";
import { toast } from "@/stores/toast";

import { usePluginUploaders, useSetUploader } from "./api";
import { useNameOf } from "./people";

const UPLOAD_ROLES = new Set(["editor", "runner", "admin"]);

export function UploadersList({ tenantId }: { tenantId: string }) {
  const ws = useWorkspace();
  const uploaders = usePluginUploaders(tenantId);
  const members = useMembers(tenantId, null);
  const project = ws.current?.project.project_id ?? null;
  const projectMembers = useMembers(tenantId, project);
  const set = useSetUploader(tenantId);
  const nameOf = useNameOf(tenantId);
  const [pick, setPick] = useState("");
  const [sure, setSure] = useState(false);
  const sureId = useId();

  const verified = useMemo(() => new Set((uploaders.data ?? []).map((u) => u.user_id)), [uploaders.data]);
  // Editors (or higher) who are not tenant admins, not you, and not confirmed yet.
  const candidates = useMemo(() => {
    const admins = new Set((members.data?.items ?? []).filter((m) => m.role === "admin" && !m.project_id).map((m) => m.principal_id));
    const seen = new Map<string, string>();
    for (const m of [...(members.data?.items ?? []), ...(projectMembers.data?.items ?? [])]) {
      if (m.principal_kind === "service_account" || !UPLOAD_ROLES.has(m.role)) continue;
      if (admins.has(m.principal_id) || verified.has(m.principal_id) || m.principal_id === ws.user.id) continue;
      seen.set(m.principal_id, m.display_name || m.email || shortId(m.principal_id));
    }
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [members.data, projectMembers.data, verified, ws.user.id]);

  const session = ws.mode === "session" && ws.me.auth === "session";
  return (
    <div data-testid="plugin-uploaders" className="grid gap-3">
      <div className="grid gap-1">
        <h3 className="text-sm font-semibold">Who can upload</h3>
        <p className="text-xs text-muted-foreground">
          Workspace admins can always upload. Editors can once an admin confirms they know who the person is. Confirming yourself isn't possible.
        </p>
      </div>
      {uploaders.isPending ? (
        <LoadingRows rows={2} />
      ) : uploaders.isError ? (
        <ErrorNotice error={uploaders.error} title="The app cannot load the confirmed uploaders" onRetry={() => void uploaders.refetch()} />
      ) : (uploaders.data ?? []).length === 0 ? (
        <p className="rounded-xl border border-dashed p-3 text-sm text-muted-foreground">No editors confirmed yet. Only admins can upload for now.</p>
      ) : (
        <ul className="grid rounded-xl border px-4" aria-label="Confirmed uploaders">
          {(uploaders.data ?? []).map((u) => (
            <li key={u.user_id} className="flex flex-wrap items-center justify-between gap-2 border-b py-2.5 last:border-b-0">
              <div className="grid min-w-0 gap-0.5">
                <span className="flex items-center gap-1.5 text-sm font-semibold [overflow-wrap:anywhere]">
                  <ShieldCheck aria-hidden className="size-4 text-success" />
                  {u.display_name || u.email || shortId(u.user_id)}
                </span>
                <span className="text-xs text-muted-foreground">
                  Confirmed by {nameOf(u.verified_by)} {relativeTime(u.verified_at)}
                  {u.method === "provider" ? " (identity provider)" : ""}
                </span>
              </div>
              <Button
                size="sm"
                variant="ghost"
                disabled={!session || set.isPending}
                aria-label={`Revoke ${u.display_name || u.email || u.user_id}`}
                onClick={() =>
                  set.mutate(
                    { userId: u.user_id, verified: false },
                    {
                      onSuccess: () => toast.success("Confirmation revoked", "They can't upload new code. What they uploaded stays."),
                      onError: (err) => toast.error("The app could not revoke the confirmation", errorMessage(err)),
                    },
                  )
                }
              >
                <UserMinus /> Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="grid gap-2 rounded-xl bg-muted p-3"
        aria-label="Confirm an uploader"
        onSubmit={(e) => {
          e.preventDefault();
          if (!pick || !sure) return;
          set.mutate(
            { userId: pick, verified: true },
            {
              onSuccess: () => {
                toast.success("Uploader confirmed", "They can upload code to this workspace now.");
                setPick("");
                setSure(false);
              },
              onError: (err) => toast.error("The app could not confirm the uploader", errorMessage(err)),
            },
          );
        }}
      >
        <Field label="Confirm an editor" htmlFor="uploader-pick" hint={candidates.length ? undefined : "Every editor is confirmed already, or the member list is empty."}>
          <NativeSelect id="uploader-pick" value={pick} disabled={!session || !candidates.length} onChange={(e) => setPick(e.target.value)} className="max-w-full">
            <option value="">Choose a person</option>
            {candidates.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <div className="flex items-start gap-2">
          <Checkbox id={sureId} checked={sure} disabled={!pick} onCheckedChange={(c) => setSure(c === true)} className="mt-0.5" />
          <label htmlFor={sureId} className="text-sm">
            I know who this person is, and they may upload code that the workspace can run.
          </label>
        </div>
        <div>
          <Button type="submit" size="sm" variant="outline" disabled={!session || !pick || !sure || set.isPending}>
            <ShieldCheck /> Confirm
          </Button>
        </div>
        {!session ? <p className="text-xs text-muted-foreground">Sign in with your browser to confirm or revoke uploaders.</p> : null}
      </form>
    </div>
  );
}
