/**
 * Builder › Access: who sees the published dashboard.
 *
 * - Client users of the project, one grant each (editors and admins grant; only admins invite
 *   new clients, plan §10.1). A grant shows a client only the published dashboard.
 * - Share links, modelled on the plot embed links: an expiry, an optional passcode (8
 *   characters or more), and the inputs the link allows. A new link is read-only (§10.3);
 *   inputs spend compute and need the runner role. The link is shown one time, then only its
 *   redacted form.
 */
import { Link2, Trash2, UserPlus, Users } from "lucide-react";
import { useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { errorMessage } from "@/api/client";
import { useAddMember, useMembers } from "@/api/queries";
import type { Membership } from "@/api/types";
import { EmptyState, ErrorNotice, Mono, QueryView, Spinner } from "@/components/common";
import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Badge, Field, Input, NativeSelect, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { ShownOnceDialog } from "@/features/integrations/shown-once";
import { absoluteTime, relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { shareUrl, useCreateShare, useDashboardGrants, useDashboardShares, useDeleteGrant, usePutGrant, useRevokeShare } from "../api";
import { Section } from "../readout/parts";
import {
  isLayout2,
  MAX_PASSCODE_LEN,
  MIN_PASSCODE_LEN,
  SHARE_INPUTS,
  SHARE_INPUTS_V1,
  type AnyDashboardSpec,
  type Dashboard,
  type DashboardGrant,
  type DashboardShare,
  type DashboardShareCreated,
  type ShareInput,
} from "../types";
import {
  DEFAULT_SHARE_FORM,
  MAX_SHARE_LABEL,
  readOnlyShareForm,
  SHARE_EXPIRY_DAYS,
  SHARE_INPUT_WORD,
  shareAccessWords,
  shareCreateBody,
  shareFormIssues,
  shareInputsOf,
  shareInputsPermission,
  type ShareForm,
} from "./spec-form";

const SHARE_INPUT_TEXT: Record<ShareInput, { label: string; hint: string; missing: string }> = {
  whatif: { label: "Let them try plan scenarios", hint: "Sliders, saved scenarios and exact results (if the dashboard offers them).", missing: "The dashboard has no what-ifs." },
  actuals: { label: "Let them upload numbers", hint: "Uploads go to the History tab for review, as the dashboard says.", missing: "The dashboard has no uploads." },
  scenario_rows: { label: "Let them try cases", hint: "They change a few values and see the prediction. Their tries stay private to them.", missing: "The dashboard has no Try a case." },
  batch: { label: "Let them score a file", hint: "They send a CSV file of new cases and download the predictions. Only they can download it.", missing: "The dashboard has no Score a file." },
};

/** The toggles a link of this dashboard shows (layout 1: the two v0.16 inputs only). */
function shareToggles(spec: AnyDashboardSpec): readonly ShareInput[] {
  if (!isLayout2(spec)) return SHARE_INPUTS_V1;
  return SHARE_INPUTS.filter((k) => k !== "whatif" || spec.preset === "mmm_planner");
}

function missingReason(spec: AnyDashboardSpec, k: ShareInput): string {
  if (k === "batch" && isLayout2(spec) && spec.preset === "image" && spec.batch?.enabled) return "Share links cannot score files on an image dashboard.";
  return SHARE_INPUT_TEXT[k].missing;
}

const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,255}$/;
const SHARE_STATUS = { active: "success", expired: "outline", revoked: "danger" } as const;

function personOf(m: { display_name?: string | null; email?: string | null; user_id?: string; principal_id?: string }): { primary: string; secondary: string | null } {
  const id = m.user_id ?? m.principal_id ?? "";
  const primary = m.display_name || m.email || id;
  return { primary, secondary: m.display_name && m.email ? m.email : null };
}

export function AccessTab({ dashboard }: { dashboard: Dashboard }) {
  return (
    <div className="grid min-w-0 gap-5">
      {dashboard.status !== "published" ? (
        <InlineMessage tone="info" role="none">
          Clients and share links see only the published dashboard. Publish it first. You can give access to clients now: they see it after you publish.
        </InlineMessage>
      ) : null}
      <ClientsSection dashboard={dashboard} />
      <SharesSection dashboard={dashboard} />
    </div>
  );
}

// ------------------------------------------------------------------------------ client grants

function ClientsSection({ dashboard }: { dashboard: Dashboard }) {
  const ws = useWorkspace();
  const current = ws.current;
  const tenantId = current?.tenant.id ?? "";
  const projectId = current?.project.project_id ?? null;
  const members = useMembers(tenantId, projectId);
  const grants = useDashboardGrants(dashboard.id);
  const put = usePutGrant(dashboard.id);
  const del = useDeleteGrant(dashboard.id);
  const canGrant = can(ws.grants, "create", "dashboard_grant");
  const canUngrant = can(ws.grants, "delete", "dashboard_grant");
  const canInvite = can(ws.grants, "create", "member");
  const [pick, setPick] = useState("");
  const [inviting, setInviting] = useState(false);
  const [removing, setRemoving] = useState<DashboardGrant | null>(null);
  const granted = new Set((grants.data?.items ?? []).map((g) => g.user_id));
  const clients = (members.data?.items ?? []).filter((m) => m.role === "client" && m.project_id != null && !granted.has(m.principal_id));
  const chosen = pick && clients.some((c) => c.principal_id === pick) ? pick : "";

  const grant = (userId: string, who: string) =>
    put.mutate(userId, {
      onSuccess: () => {
        toast.success("Access given", `${who} can see the dashboard${dashboard.status === "published" ? "" : " after you publish it"}.`);
        setPick("");
      },
      onError: (e) => toast.error("The app could not give access", errorMessage(e)),
    });

  return (
    <Section
      title="Clients with access"
      description="Client users sign in and see only the published dashboards that you give them. Editors and admins give access. Only admins invite new clients."
      action={
        <Button size="sm" variant="secondary" disabled={!canInvite} title={canInvite ? undefined : "Only admins invite new clients."} onClick={() => setInviting(true)}>
          <UserPlus /> Invite a client
        </Button>
      }
    >
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const m = clients.find((c) => c.principal_id === chosen);
          if (m) grant(m.principal_id, personOf(m).primary);
        }}
      >
        <Field label="Client user" htmlFor="ds-grant-user" className="min-w-64 flex-1" hint={members.isSuccess && !clients.length ? "All the project's clients have access. Invite a client to add one." : undefined}>
          <NativeSelect id="ds-grant-user" value={chosen} disabled={!canGrant || !clients.length} onChange={(e) => setPick(e.target.value)}>
            <option value="">{members.isPending ? "Loading…" : clients.length ? "Choose a client" : "No other clients"}</option>
            {clients.map((m) => (
              <option key={m.principal_id} value={m.principal_id}>
                {personOf(m).primary}
                {personOf(m).secondary ? ` (${personOf(m).secondary})` : ""}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Button type="submit" disabled={!canGrant || !chosen || put.isPending}>
          {put.isPending ? <Spinner /> : <Users />} Give access
        </Button>
      </form>
      <QueryView query={grants}>
        {(page) =>
          page.items.length ? (
            <div className="min-w-0 overflow-x-auto">
              <Table aria-label="Clients with access">
                <THead>
                  <TR>
                    <TH>Client</TH>
                    <TH>Since</TH>
                    <TH>
                      <span className="sr-only">Actions</span>
                    </TH>
                  </TR>
                </THead>
                <TBody>
                  {page.items.map((g) => {
                    const p = personOf(g);
                    return (
                      <TR key={g.user_id} data-grant={g.user_id}>
                        <TD>
                          <span className="block font-medium [overflow-wrap:anywhere]">{p.primary}</span>
                          {p.secondary ? <span className="block text-xs text-muted-foreground">{p.secondary}</span> : null}
                        </TD>
                        <TD title={absoluteTime(g.created_at)}>{relativeTime(g.created_at)}</TD>
                        <TD className="text-right">
                          <Button variant="ghost" size="sm" disabled={!canUngrant} onClick={() => setRemoving(g)} aria-label={`Remove access of ${p.primary}`}>
                            <Trash2 /> Remove
                          </Button>
                        </TD>
                      </TR>
                    );
                  })}
                </TBody>
              </Table>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No client has access to this dashboard.</p>
          )
        }
      </QueryView>
      {inviting ? (
        <InviteClientDialog
          tenantId={tenantId}
          projectId={projectId}
          where={current?.project.name ?? "this project"}
          onClose={() => setInviting(false)}
          onInvited={(m) => {
            setInviting(false);
            if (canGrant) grant(m.principal_id, personOf(m).primary);
          }}
        />
      ) : null}
      {removing ? (
        <Dialog open onOpenChange={(o) => !o && setRemoving(null)}>
          <DialogContent>
            <DialogTitle>Remove the access of {personOf(removing).primary}?</DialogTitle>
            <DialogDescription>From their next page load, they do not see this dashboard. Their client role in the project stays.</DialogDescription>
            {del.isError ? <ErrorNotice error={del.error} /> : null}
            <DialogFooter>
              <Button variant="ghost" onClick={() => setRemoving(null)}>
                Keep access
              </Button>
              <Button
                variant="destructive"
                disabled={del.isPending}
                onClick={() =>
                  del.mutate(removing.user_id, {
                    onSuccess: () => {
                      toast.success("Access removed", personOf(removing).primary);
                      setRemoving(null);
                    },
                  })
                }
              >
                <Trash2 /> Remove access
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </Section>
  );
}

/** Members › Add member with the client role fixed (admins only). */
function InviteClientDialog({ tenantId, projectId, where, onClose, onInvited }: { tenantId: string; projectId: string | null; where: string; onClose: () => void; onInvited: (m: Membership) => void }) {
  const add = useAddMember(tenantId, projectId);
  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const value = email.trim().toLowerCase();
  const error = submitted && !(value.length <= 320 && EMAIL_RE.test(value)) ? "Type an e-mail address, for example ana@example.com." : undefined;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (!(value.length <= 320 && EMAIL_RE.test(value))) return;
    add.mutate(
      { email: value, role: "client" },
      {
        onSuccess: (m) => {
          toast.success("Client invited", `${value} gets access the next time that they sign in.`);
          onInvited(m);
        },
      },
    );
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Invite a client to {where}</DialogTitle>
        <DialogDescription>The person gets the client role in this project. A client sees only the dashboards that you give them, and nothing else of the project. The app gives them access to this dashboard too.</DialogDescription>
        <form className="grid gap-4" onSubmit={submit} noValidate>
          <Field label="E-mail" htmlFor="ds-invite-email" error={error}>
            <Input id="ds-invite-email" type="email" autoFocus autoComplete="off" placeholder="cleo@client.example" value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          {add.isError ? <ErrorNotice error={add.error} title="The app could not invite the client" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={add.isPending}>
              {add.isPending ? <Spinner /> : <UserPlus />} Invite client
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------------------ share links

function SharesSection({ dashboard }: { dashboard: Dashboard }) {
  const ws = useWorkspace();
  const canCreate = can(ws.grants, "create", "dashboard_share");
  const canRevoke = can(ws.grants, "delete", "dashboard_share");
  const shares = useDashboardShares(dashboard.id);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<DashboardShareCreated | null>(null);
  const [revoking, setRevoking] = useState<DashboardShare | null>(null);
  const published = dashboard.status === "published";
  return (
    <Section
      title="Share links"
      description="Anyone with a link sees the published dashboard without signing in, until the link ends or you revoke it. New links are read-only."
      action={
        <Button size="sm" disabled={!canCreate || !published} title={!published ? "Publish the dashboard first." : canCreate ? undefined : "Only editors and admins create links."} onClick={() => setCreating(true)}>
          <Link2 /> Create link
        </Button>
      }
    >
      <QueryView query={shares}>
        {(page) =>
          page.items.length ? (
            <div className="min-w-0 overflow-x-auto">
              <Table aria-label="Share links">
                <THead>
                  <TR>
                    <TH>Link</TH>
                    <TH>Status</TH>
                    <TH>Allows</TH>
                    <TH>Expires</TH>
                    <TH>Views</TH>
                    <TH>
                      <span className="sr-only">Actions</span>
                    </TH>
                  </TR>
                </THead>
                <TBody>
                  {page.items.map((s) => (
                    <TR key={s.id} data-share={s.id} data-status={s.status}>
                      <TD>
                        <span className="font-medium">{s.label ?? "Unnamed link"}</span>
                        <Mono className="block text-muted-foreground">{s.display}</Mono>
                        {s.has_passcode ? <span className="block text-xs text-muted-foreground">With a passcode</span> : null}
                      </TD>
                      <TD>
                        <Badge variant={SHARE_STATUS[s.status]}>{s.status}</Badge>
                        {s.locked_until && Date.parse(s.locked_until) > Date.now() ? <span className="block text-xs text-warning">Locked after wrong passcodes</span> : null}
                      </TD>
                      <TD className="text-sm">{shareAccessWords(s.allowed_inputs)}</TD>
                      <TD title={absoluteTime(s.expires_at)}>{s.status === "revoked" ? `revoked ${relativeTime(s.revoked_at)}` : relativeTime(s.expires_at)}</TD>
                      <TD className="tabular">
                        {s.views ?? 0}
                        {s.last_viewed_at ? <span className="block text-xs text-muted-foreground">last {relativeTime(s.last_viewed_at)}</span> : null}
                      </TD>
                      <TD className="text-right">
                        {s.status === "active" ? (
                          <Button variant="ghost" size="sm" disabled={!canRevoke} onClick={() => setRevoking(s)} aria-label={`Revoke ${s.label ?? s.display}`}>
                            <Trash2 /> Revoke
                          </Button>
                        ) : null}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
          ) : (
            <EmptyState icon={<Link2 />} title="This dashboard has no share links">
              Create a link for a person who does not sign in, for example for a board pack.
            </EmptyState>
          )
        }
      </QueryView>
      {creating ? (
        <CreateShareDialog
          dashboard={dashboard}
          onClose={() => setCreating(false)}
          onCreated={(c) => {
            setCreating(false);
            setCreated(c);
          }}
        />
      ) : null}
      {created ? <ShareCreatedDialog created={created} onClose={() => setCreated(null)} /> : null}
      {revoking ? <RevokeShareDialog dashboardId={dashboard.id} share={revoking} onClose={() => setRevoking(null)} /> : null}
    </Section>
  );
}

export function CreateShareDialog({ dashboard, onClose, onCreated }: { dashboard: Dashboard; onClose: () => void; onCreated: (c: DashboardShareCreated) => void }) {
  const ws = useWorkspace();
  const create = useCreateShare(dashboard.id);
  const [form, setForm] = useState<ShareForm>({ ...DEFAULT_SHARE_FORM });
  const [submitted, setSubmitted] = useState(false);
  const spec = dashboard.published_revision?.spec ?? dashboard.draft_spec;
  const available = shareInputsOf(spec);
  const inputs = shareInputsPermission(ws.grants.projectRole);
  const issues = shareFormIssues(form);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (Object.keys(issues).length) return;
    create.mutate(shareCreateBody(inputs.allowed ? form : readOnlyShareForm(form), available), { onSuccess: onCreated });
  };
  const set = (patch: Partial<ShareForm>) => setForm((f) => ({ ...f, ...patch }));
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>Share “{spec.title}” by link</DialogTitle>
        <DialogDescription>Anyone with the link can read the published dashboard until it ends or you revoke it. They do not sign in. Add a passcode for numbers that are not public, and send the passcode separately.</DialogDescription>
        <form className="grid gap-5" onSubmit={submit} noValidate>
          <div className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <Field label="Label (optional)" htmlFor="ds-share-label" error={submitted ? issues.label : undefined} hint="Who gets the link. Only your team sees the label.">
              <Input id="ds-share-label" autoFocus maxLength={MAX_SHARE_LABEL} placeholder="Board pack" value={form.label} onChange={(e) => set({ label: e.target.value })} />
            </Field>
            <Field label="Ends after" htmlFor="ds-share-days" error={submitted ? issues.days : undefined}>
              <NativeSelect id="ds-share-days" value={form.days} onChange={(e) => set({ days: Number(e.target.value) })}>
                {SHARE_EXPIRY_DAYS.map((d) => (
                  <option key={d} value={d}>
                    {d} days{d === DEFAULT_SHARE_FORM.days ? " (default)" : ""}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          </div>
          <Field
            label="Passcode (optional)"
            htmlFor="ds-share-pass"
            error={submitted ? issues.passcode : undefined}
            hint={`${MIN_PASSCODE_LEN} to ${MAX_PASSCODE_LEN} characters. After 5 wrong tries the link locks for 15 minutes. You cannot change a passcode later: create a new link.`}
          >
            <Input id="ds-share-pass" type="password" autoComplete="new-password" maxLength={MAX_PASSCODE_LEN} value={form.passcode} onChange={(e) => set({ passcode: e.target.value })} />
          </Field>
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-sm font-semibold">What the link allows</legend>
            <p className="text-xs text-muted-foreground">The link can always read the dashboard. Inputs run as you and use your project's compute.</p>
            {shareToggles(spec).map((k) => {
              const has = available.includes(k);
              const id = `ds-share-${k}`;
              const why = !has ? missingReason(spec, k) : !inputs.allowed ? inputs.reason : null;
              return (
                <div key={k} className="flex items-start gap-2 text-sm">
                  <Checkbox id={id} className="mt-0.5" checked={has && inputs.allowed && form[k]} disabled={!has || !inputs.allowed} onCheckedChange={(on) => set({ [k]: on === true })} aria-describedby={`${id}-hint`} />
                  <span className="grid gap-0.5">
                    <label htmlFor={id} className="font-medium">
                      {SHARE_INPUT_TEXT[k].label}
                    </label>
                    <span id={`${id}-hint`} className="text-xs text-muted-foreground">
                      {why ?? SHARE_INPUT_TEXT[k].hint}
                    </span>
                  </span>
                </div>
              );
            })}
            <p className="text-xs text-muted-foreground" data-testid="share-access-summary">
              This link: {shareAccessWords(shareCreateBody(inputs.allowed ? form : readOnlyShareForm(form), available).allowed_inputs)}.
            </p>
          </fieldset>
          {create.isError ? <ErrorNotice error={create.error} title="The link was not created" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : <Link2 />} Create link
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** The link with its token, one time: the token lives in this dialog's props only. */
export function ShareCreatedDialog({ created, onClose }: { created: DashboardShareCreated; onClose: () => void }) {
  const allows = (created.allowed_inputs ?? []).map((a) => SHARE_INPUT_WORD[a].toLowerCase());
  return (
    <ShownOnceDialog
      title="Copy the link now"
      description={
        <>
          The app shows the link only one time, because it contains its own token. After you close this window, the list shows only {created.display}. The link works until {absoluteTime(created.expires_at)}
          {created.has_passcode ? " and asks for the passcode" : ""}. It {allows.length ? `allows ${allows.join(" and ")}` : "is read-only"}.
        </>
      }
      label="Share link"
      value={shareUrl(created.url_fragment)}
      copyLabel="Copy share link"
      acknowledge="I have copied the link"
      onClose={onClose}
    />
  );
}

function RevokeShareDialog({ dashboardId, share, onClose }: { dashboardId: string; share: DashboardShare; onClose: () => void }) {
  const revoke = useRevokeShare(dashboardId);
  const name = share.label ?? share.display;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Revoke “{name}”?</DialogTitle>
        <DialogDescription>From the next request, the link shows “This link has ended or was removed”. You cannot undo this. To share again, create a new link.</DialogDescription>
        {revoke.isError ? <ErrorNotice error={revoke.error} /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            disabled={revoke.isPending}
            onClick={() =>
              revoke.mutate(share.id, {
                onSuccess: () => {
                  toast.success("Link revoked", name);
                  onClose();
                },
              })
            }
          >
            <Trash2 /> Revoke link
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
