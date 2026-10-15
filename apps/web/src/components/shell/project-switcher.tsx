/**
 * Tenant and project switcher (header). The choice persists per browser; switching cancels
 * the previous project's requests, remounts the page and returns to the section's list, so
 * nothing from one project is shown under another.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouterState } from "@tanstack/react-router";
import { Building2, Check, ChevronsUpDown, FolderPlus, Users } from "lucide-react";
import { useState } from "react";

import { atLeast, ROLE_META } from "@/api/access";
import { errorMessage } from "@/api/client";
import { useCreateProject } from "@/api/queries";
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
import { Badge, Field, Input, Textarea } from "@/components/ui/primitives";
import { useWorkspace, type ProjectChoice } from "@/features/auth/workspace";
import { cn } from "@/lib/utils";
import { useScope } from "@/stores/scope";
import { toast } from "@/stores/toast";

/** Where to land after a switch: the list page of the current section. */
export function sectionRoot(pathname: string): string {
  const first = pathname.split("/").filter(Boolean)[0];
  if (!first) return "/";
  return `/${first}`;
}

export function useSwitchProject(): (choice: ProjectChoice) => void {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return (choice) => {
    const current = useScope.getState().active;
    if (current && current.tenantId === choice.project.tenant_id && current.projectId === choice.project.project_id) return;
    void qc.cancelQueries({ queryKey: ["p"] });
    useScope.getState().select({ tenantId: choice.project.tenant_id, projectId: choice.project.project_id });
    void navigate({ to: sectionRoot(pathname) });
    toast.info(`Switched to ${choice.project.name}`, choice.tenant.name);
  };
}

const TIER_LABEL: Record<string, string> = {
  internal: "internal",
  verified: "verified",
  public: "public",
  "self-hosted-trusted": "self-hosted",
};

export function ProjectSwitcher({ className }: { className?: string }) {
  const ws = useWorkspace();
  const switchTo = useSwitchProject();
  const [creating, setCreating] = useState<string | null>(null);
  const navigate = useNavigate();
  const current = ws.current;
  const tenants = [...new Map(ws.choices.map((c) => [c.tenant.id, c])).values()];
  const label = current ? `${current.tenant.name} / ${current.project.name}` : "No project";

  if (ws.mode === "none") return null;
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={`Project: ${label}. Switch project`}
            className={cn(
              "group flex h-8 max-w-[9.5rem] min-w-0 items-center sm:max-w-[16rem] lg:max-w-[19rem] xl:max-w-[26rem] gap-2 rounded-md border bg-card px-2 text-left text-[13px] shadow-xs hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring",
              className,
            )}
          >
            <span aria-hidden className="grid size-5 shrink-0 place-items-center rounded bg-primary/12 text-[11px] font-semibold text-primary uppercase">
              {(current?.tenant.name ?? "?").slice(0, 1)}
            </span>
            <span className="min-w-0 truncate">
              <span className="hidden text-muted-foreground sm:inline">{current?.tenant.name ?? "—"} / </span>
              <span className="font-medium">{current?.project.name ?? "No project"}</span>
            </span>
            {current ? <span className="hidden text-[11px] text-muted-foreground lg:inline">{current.role}</span> : null}
            <ChevronsUpDown className="ml-auto size-3.5 shrink-0 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-[70vh] w-72 overflow-y-auto">
          {tenants.length === 0 ? <DropdownMenuLabel>You are not a member of any project.</DropdownMenuLabel> : null}
          {tenants.map((t, i) => {
            const projects = ws.choices.filter((c) => c.tenant.id === t.tenant.id);
            return (
              <div key={t.tenant.id} role="group" aria-label={t.tenant.name}>
                {i > 0 ? <DropdownMenuSeparator /> : null}
                <DropdownMenuLabel className="flex items-center gap-2">
                  <Building2 className="size-3.5" />
                  <span className="truncate font-medium text-foreground">{t.tenant.name}</span>
                  <Badge variant="outline" className="ml-auto text-[10px]">
                    {TIER_LABEL[t.tenant.trust_tier] ?? t.tenant.trust_tier}
                  </Badge>
                  {t.tenant.status && t.tenant.status !== "active" ? (
                    <Badge variant="warning" className="text-[10px]">
                      {t.tenant.status}
                    </Badge>
                  ) : null}
                </DropdownMenuLabel>
                {projects.map((c) => {
                  const selected = c.key === current?.key;
                  return (
                    <DropdownMenuItem key={c.key} onSelect={() => switchTo(c)} aria-current={selected ? "true" : undefined} className="relative pl-7">
                      <span className="absolute left-2.5 flex size-3.5 items-center">{selected ? <Check className="size-3.5 text-primary" /> : null}</span>
                      <span className="min-w-0 flex-1 truncate">{c.project.name}</span>
                      <span className="text-[11px] text-muted-foreground" title={ROLE_META[c.role].description}>
                        {c.role}
                      </span>
                    </DropdownMenuItem>
                  );
                })}
                {atLeast(t.tenantRole, "admin") && t.tenant.status === "active" ? (
                  <DropdownMenuItem onSelect={() => setCreating(t.tenant.id)} className="pl-7 text-muted-foreground">
                    <FolderPlus /> New project in {t.tenant.name}
                  </DropdownMenuItem>
                ) : null}
              </div>
            );
          })}
          {current ? (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => void navigate({ to: "/members" })}>
                <Users /> Members of {current.project.name}
              </DropdownMenuItem>
            </>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
      {creating ? <NewProjectDialog tenantId={creating} onClose={() => setCreating(null)} /> : null}
    </>
  );
}

const SLUG_RE = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/;

export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/g, "");
}

function NewProjectDialog({ tenantId, onClose }: { tenantId: string; onClose: () => void }) {
  const create = useCreateProject(tenantId);
  const ws = useWorkspace();
  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [description, setDescription] = useState("");
  const effectiveId = idTouched ? id : slugify(name);
  const idError = effectiveId && !SLUG_RE.test(effectiveId) ? "Lowercase letters, digits, - and _ (start and end with a letter or digit)." : undefined;
  const tenant = ws.choices.find((c) => c.tenant.id === tenantId)?.tenant;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>New project in {tenant?.name ?? tenantId}</DialogTitle>
        <DialogDescription>Projects hold datasets, specs, runs and models. You become its admin through your organisation role.</DialogDescription>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!name.trim() || !effectiveId || idError) return;
            create.mutate(
              { id: effectiveId, name: name.trim(), ...(description.trim() ? { description: description.trim() } : {}) },
              {
                onSuccess: (p) => {
                  toast.success("Project created", `${p.name} (${tenantId}/${p.id})`);
                  onClose();
                },
                onError: (err) => toast.error("The project could not be created", errorMessage(err)),
              },
            );
          }}
        >
          <Field label="Name" htmlFor="np-name">
            <Input id="np-name" required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </Field>
          <Field label="Id" htmlFor="np-id" hint={`Used in refs such as dataset://${effectiveId || "growth"}/…; cannot be changed.`} error={idError}>
            <Input
              id="np-id"
              className="font-mono"
              value={effectiveId}
              onChange={(e) => {
                setIdTouched(true);
                setId(e.target.value);
              }}
            />
          </Field>
          <Field label="Description (optional)" htmlFor="np-desc">
            <Textarea id="np-desc" maxLength={4000} value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !name.trim() || !effectiveId || Boolean(idError)}>
              Create project
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
