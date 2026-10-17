/**
 * Members of the selected project, and of its organisation (tenant-wide grants apply to every
 * project). Roles are ordered viewer < editor < runner < admin; each includes the last.
 */
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { Building2, Trash2, UserPlus, Users } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { can, ROLE_META, ROLES } from "@/api/access";
import { errorMessage } from "@/api/client";
import { useAddMember, useMembers, useRemoveMember, useUpdateMember } from "@/api/queries";
import type { Membership, ProjectRole } from "@/api/types";
import { EmptyState, ErrorNotice, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, SegmentedControl } from "@/components/ui/overlays";
import { Badge, Card, CardContent, Field, Input, NativeSelect } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { ReviewerToggle } from "@/features/governance/reviewer-toggle";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

const route = getRouteApi("/members");

const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,255}$/;
const PRINCIPAL_RE = /^(?:usr|sa)_[0-9a-z]{26}$/;

/** Turn the invite field into a `MembershipCreate` target, or an error message. */
export function parseInvitee(value: string): { principal_id: string } | { email: string } | { error: string } {
  const v = value.trim();
  if (!v) return { error: "Type an e-mail address or a user id." };
  if (PRINCIPAL_RE.test(v)) return { principal_id: v };
  if (v.length <= 320 && EMAIL_RE.test(v)) return { email: v.toLowerCase() };
  return { error: "The value is not correct. Type an e-mail address (ana@example.com) or a user id (usr_…)." };
}

function personLabel(m: Membership): { primary: string; secondary: string | null } {
  const primary = m.display_name || m.email || m.principal_id;
  const secondary = m.display_name && m.email ? m.email : m.display_name || m.email ? m.principal_id : null;
  return { primary, secondary };
}

export function MembersPage() {
  const ws = useWorkspace();
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/members" });
  const current = ws.current;
  const tenantWide = search.scope === "tenant";
  const tenantId = current?.tenant.id ?? "";
  const projectId = tenantWide ? null : (current?.project.project_id ?? null);
  const members = useMembers(tenantId, projectId);
  const resource = tenantWide ? "tenant_member" : "member";
  const canAdd = can(ws.grants, "create", resource);
  const canEdit = can(ws.grants, "update", resource);
  const canRemove = can(ws.grants, "delete", resource);
  const canSeeTenant = can(ws.grants, "read", "tenant_member");
  const [inviting, setInviting] = useState(false);
  const [removing, setRemoving] = useState<Membership | null>(null);
  const update = useUpdateMember(tenantId, projectId);

  const columns = useMemo<ColumnDef<Membership, unknown>[]>(
    () => [
      {
        id: "person",
        header: "Member",
        accessorFn: (m) => `${m.display_name ?? ""} ${m.email ?? ""} ${m.principal_id}`,
        cell: ({ row }) => {
          const { primary, secondary } = personLabel(row.original);
          return (
            <div className="grid min-w-0">
              <span className="truncate font-medium">{primary}</span>
              {secondary ? <span className="truncate text-xs text-muted-foreground">{secondary}</span> : null}
            </div>
          );
        },
      },
      {
        id: "kind",
        header: "Kind",
        accessorFn: (m) => m.principal_kind ?? "user",
        cell: ({ getValue }) => <span className="text-muted-foreground">{getValue() === "service_account" ? "Service account" : "Person"}</span>,
      },
      {
        accessorKey: "role",
        header: "Role",
        sortingFn: (a, b) => ROLES.indexOf(a.original.role) - ROLES.indexOf(b.original.role),
        cell: ({ row }) => {
          const m = row.original;
          const inherited = !tenantWide && m.project_id == null;
          if (!canEdit || inherited) {
            return (
              <span className="inline-flex items-center gap-1.5">
                <Badge variant={m.role === "admin" ? "default" : "secondary"}>{ROLE_META[m.role].label}</Badge>
                {inherited ? (
                  <Badge variant="outline" title="The organisation gives this role. Change it on the Organisation tab.">
                    <Building2 /> organisation
                  </Badge>
                ) : null}
              </span>
            );
          }
          return (
            <NativeSelect
              aria-label={`Role of ${personLabel(m).primary}`}
              className="h-8 w-32"
              value={m.role}
              disabled={update.isPending}
              onChange={(e) => {
                const role = e.target.value as ProjectRole;
                update.mutate(
                  { principalId: m.principal_id, role },
                  {
                    onSuccess: () => toast.success("Role changed", `${personLabel(m).primary} is now ${ROLE_META[role].label.toLowerCase()}`),
                    onError: (err) => toast.error("The app could not change the role", errorMessage(err)),
                  },
                );
              }}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {ROLE_META[r].label}
                </option>
              ))}
            </NativeSelect>
          );
        },
      },
      {
        // M3.5: the reviewer capability (approves model validations).
        id: "reviewer",
        header: "Reviewer",
        accessorFn: (m) => (m.capabilities ?? []).includes("reviewer"),
        cell: ({ row }) => {
          const m = row.original;
          const inherited = !tenantWide && m.project_id == null;
          return (
            <ReviewerToggle
              member={m}
              name={personLabel(m).primary}
              editable={canEdit && !inherited}
              pending={update.isPending}
              onChange={(capabilities, done) => update.mutate({ principalId: m.principal_id, capabilities }, done)}
            />
          );
        },
      },
      {
        accessorKey: "created_at",
        header: "Added",
        cell: ({ row }) => <span className="text-muted-foreground">{relativeTime(row.original.created_at)}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">Actions</span>,
        enableSorting: false,
        cell: ({ row }) =>
          canRemove && (tenantWide || row.original.project_id != null) && row.original.principal_id !== ws.user.id ? (
            <Button variant="ghost" size="sm" aria-label={`Remove ${personLabel(row.original).primary}`} onClick={() => setRemoving(row.original)}>
              <Trash2 /> Remove
            </Button>
          ) : null,
      },
    ],
    [canEdit, canRemove, tenantWide, update, ws.user.id],
  );

  if (!current) return null;
  const where = tenantWide ? current.tenant.name : current.project.name;
  return (
    <>
      <PageHeader
        title="Members"
        description={
          tenantWide ? (
            <>Organisation-wide roles apply to every project in {current.tenant.name}.</>
          ) : (
            <>
              People and service accounts who can work in <span className="font-medium text-foreground">{current.project.name}</span>.
            </>
          )
        }
        actions={
          <>
            {canSeeTenant ? (
              <SegmentedControl<"project" | "tenant">
                label="Member scope"
                value={tenantWide ? "tenant" : "project"}
                onValueChange={(v) => void navigate({ search: { scope: v === "tenant" ? "tenant" : undefined } })}
                options={[
                  { value: "project", label: "This project" },
                  { value: "tenant", label: "Organisation" },
                ]}
              />
            ) : null}
            <Button onClick={() => setInviting(true)} disabled={!canAdd} title={canAdd ? undefined : "Only admins can add members"}>
              <UserPlus /> Add member
            </Button>
          </>
        }
      />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_18rem]">
        <QueryView query={members}>
          {(page) =>
            page.items.length === 0 ? (
              <EmptyState icon={<Users />} title={`No person has a role in ${where}`}>
                {canAdd ? "Add people by e-mail. They get access the next time that they sign in." : "Ask an admin to add members."}
              </EmptyState>
            ) : (
              <DataTable
                label={`Members of ${where}`}
                data={page.items}
                columns={columns}
                getRowId={(m) => `${m.principal_id}:${m.project_id ?? "*"}`}
                filterPlaceholder="Filter members"
                initialSorting={[{ id: "role", desc: true }]}
              />
            )
          }
        </QueryView>
        <RoleLegend current={current.role} />
      </div>
      {inviting ? <InviteDialog tenantId={current.tenant.id} projectId={projectId} where={where} onClose={() => setInviting(false)} /> : null}
      {removing ? <RemoveDialog member={removing} tenantId={current.tenant.id} projectId={projectId} where={where} onClose={() => setRemoving(null)} /> : null}
    </>
  );
}

function RoleLegend({ current }: { current: ProjectRole }) {
  return (
    <Card aria-label="What each role can do">
      <CardContent className="grid gap-3 pt-4">
        <p className="text-[13px] font-medium">Roles</p>
        <ol className="grid gap-2.5">
          {ROLES.map((r) => (
            <li key={r} className="grid gap-0.5">
              <span className="flex items-center gap-2 text-[13px] font-medium">
                {ROLE_META[r].label}
                {r === current ? <Badge variant="info">you</Badge> : null}
              </span>
              <span className="text-xs text-muted-foreground">{ROLE_META[r].description}</span>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

function InviteDialog({ tenantId, projectId, where, onClose }: { tenantId: string; projectId: string | null; where: string; onClose: () => void }) {
  const add = useAddMember(tenantId, projectId);
  const [who, setWho] = useState("");
  const [role, setRole] = useState<ProjectRole>("viewer");
  const [submitted, setSubmitted] = useState(false);
  const target = parseInvitee(who);
  const error = submitted && "error" in target ? target.error : undefined;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if ("error" in target) return;
    add.mutate(
      { ...target, role },
      {
        onSuccess: () => {
          toast.success("Member added", "email" in target ? `${target.email} gets access the next time that they sign in.` : undefined);
          onClose();
        },
      },
    );
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Add a member to {where}</DialogTitle>
        <DialogDescription>Invite a person by e-mail, or add a known user id. The invitation becomes active when the person first signs in with that verified address.</DialogDescription>
        <form className="grid gap-4" onSubmit={submit} noValidate>
          <Field label="E-mail or user id" htmlFor="inv-who" error={error}>
            <Input id="inv-who" autoFocus autoComplete="off" placeholder="ana@example.com" value={who} onChange={(e) => setWho(e.target.value)} />
          </Field>
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-[13px] font-medium">Role</legend>
            {ROLES.map((r) => (
              <label key={r} className={cn("flex cursor-pointer items-start gap-3 rounded-lg border p-3 hover:bg-accent/40", role === r && "border-primary/50 bg-accent/40")}>
                <input type="radio" name="inv-role" value={r} checked={role === r} onChange={() => setRole(r)} className="mt-1 accent-[var(--primary)]" />
                <span className="grid gap-0.5">
                  <span className="text-[13px] font-medium">{ROLE_META[r].label}</span>
                  <span className="text-xs text-muted-foreground">{ROLE_META[r].description}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {add.isError ? <ErrorNotice error={add.error} title="The app could not add the member" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={add.isPending}>
              <UserPlus /> Add member
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RemoveDialog({ member, tenantId, projectId, where, onClose }: { member: Membership; tenantId: string; projectId: string | null; where: string; onClose: () => void }) {
  const remove = useRemoveMember(tenantId, projectId);
  const { primary } = personLabel(member);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Remove {primary} from {where}?</DialogTitle>
        <DialogDescription>
          Their {member.role} role stops now. Their API tokens for {where} stop working. Their runs and models stay. <Mono>{member.principal_id}</Mono>
        </DialogDescription>
        {remove.isError ? <ErrorNotice error={remove.error} title="The app could not remove the member" /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={remove.isPending}
            onClick={() =>
              remove.mutate(member.principal_id, {
                onSuccess: () => {
                  toast.success("Member removed", primary);
                  onClose();
                },
              })
            }
          >
            <Trash2 /> Remove
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
