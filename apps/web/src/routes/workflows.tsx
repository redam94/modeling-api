/**
 * Workflows (M3.5): the list and the template gallery, the "use this template" wizard, the
 * Guided recipe flow, a workflow's editor / runs / automation / revisions, and the run monitor.
 * The React Flow canvas loads only with the editor and the run graph (lazy chunk).
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { Archive, GitBranchPlus, Play, Plus, Workflow as WorkflowIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { can } from "@/api/access";
import { ApiError } from "@/api/client";
import { EmptyState, ErrorNotice, LoadingRows, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Field, Input } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { useEffectivePersona } from "@/features/guided/persona";
import { useCreateWorkflow, useCreateWorkflowRevision, useNodeCatalog, useUpdateWorkflow, useWorkflow, useWorkflowRevision, useWorkflowRuns, useWorkflows } from "@/features/workflows/api";
import { AutomationPanel } from "@/features/workflows/automation";
import { emptyWorkflow } from "@/features/workflows/doc";
import { WorkflowEditor, type SaveRequest } from "@/features/workflows/editor";
import { RevisionsPanel } from "@/features/workflows/revisions";
import { RunDialog } from "@/features/workflows/run-dialog";
import { WorkflowRunMonitor } from "@/features/workflows/run-monitor";
import { RUN_STATUS_META, TONE_BADGE, TRIGGER_LABEL } from "@/features/workflows/status";
import { RecipePicker, RecipeRunner, TemplateGallery, TemplateWizard } from "@/features/workflows/templates";
import type { Workflow, WorkflowDoc, WorkflowRun } from "@/features/workflows/types";
import { runCostView } from "@/lib/cost";
import { elapsedSeconds, formatDuration, relativeTime, shortId } from "@/lib/format";
import { toast } from "@/stores/toast";

const listRoute = getRouteApi("/workflows");
const newRoute = getRouteApi("/workflows/new");
const recipeRoute = getRouteApi("/workflows/recipe");
const detailRoute = getRouteApi("/workflows/$workflowId");
const runRoute = getRouteApi("/workflow-runs/$runId");

const NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

// ----------------------------------------------------------------------------- list

const workflowColumns: ColumnDef<Workflow, unknown>[] = [
  {
    accessorKey: "name",
    header: "Workflow",
    cell: ({ row }) => (
      <Link to="/workflows/$workflowId" params={{ workflowId: row.original.id }} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
        {row.original.name}
      </Link>
    ),
  },
  { accessorKey: "description", header: "What it does", cell: ({ row }) => <span className="text-muted-foreground">{row.original.description ?? "—"}</span> },
  { accessorKey: "template", header: "From", cell: ({ row }) => (row.original.template ? <Badge variant="outline">{row.original.template}</Badge> : <span className="text-muted-foreground">blank</span>) },
  { accessorKey: "latest_revision", header: "Revision", cell: ({ row }) => <Mono>r{row.original.latest_revision}</Mono> },
  { accessorKey: "updated_at", header: "Updated", cell: ({ row }) => <span title={row.original.updated_at}>{relativeTime(row.original.updated_at)}</span> },
];

export function WorkflowsPage() {
  const search = listRoute.useSearch();
  const navigate = useNavigate({ from: "/workflows" });
  const persona = useEffectivePersona();
  const ws = useWorkspace();
  const guided = persona === "guided";
  const tab = search.tab ?? (guided ? "templates" : "workflows");
  const workflows = useWorkflows();
  const mayCreate = can(ws.grants, "create", "workflow");
  return (
    <>
      <PageHeader
        title="Workflows"
        description={
          guided
            ? "Ready-made recipes do a check of your data, fit a model and explain each decision. Select one and run it on your data."
            : "A workflow puts tests, data processing, train/test plans, models, governance checks and reports in one versioned graph. The graph runs the same way each time."
        }
        actions={
          !guided && mayCreate ? (
            <Button asChild>
              <Link to="/workflows/new">
                <Plus /> New workflow
              </Link>
            </Button>
          ) : null
        }
      />
      <Tabs value={tab} onValueChange={(v) => void navigate({ search: { tab: v as "workflows" | "templates" } })}>
        <TabsList>
          <TabsTrigger value="templates">{guided ? "Recipes" : "Templates"}</TabsTrigger>
          <TabsTrigger value="workflows">{guided ? "Your workflows" : "Workflows"}</TabsTrigger>
        </TabsList>
        <TabsContent value="templates">
          <TemplateGallery guided={guided} />
        </TabsContent>
        <TabsContent value="workflows">
          <QueryView query={workflows}>
            {(page) =>
              page.items.length ? (
                <DataTable label="Workflows" data={page.items} columns={workflowColumns} getRowId={(w) => w.id} onRowClick={(w) => void navigate({ to: "/workflows/$workflowId", params: { workflowId: w.id } })} />
              ) : (
                <EmptyState
                  icon={<WorkflowIcon />}
                  title="No workflows yet"
                  action={
                    <Button variant="outline" onClick={() => void navigate({ search: { tab: "templates" } })}>
                      Browse the templates
                    </Button>
                  }
                >
                  Start from a template. The readiness-to-ARIMAX recipe is a good first template.
                </EmptyState>
              )
            }
          </QueryView>
        </TabsContent>
      </Tabs>
    </>
  );
}

// ----------------------------------------------------------------------------- new

function BlankWorkflow() {
  const navigate = useNavigate();
  const ws = useWorkspace();
  const catalog = useNodeCatalog();
  const workflows = useWorkflows();
  const create = useCreateWorkflow();
  const [name, setName] = useState("");
  const [started, setStarted] = useState<WorkflowDoc | null>(null);
  const taken = workflows.data?.items.some((w) => w.name === name) ?? false;
  const nameError = name && !NAME_RE.test(name) ? "Use only lowercase letters, digits and -. Start and end with a letter or a digit." : taken ? "The project already has a workflow with this name." : null;
  const onSave = useCallback(
    async ({ doc, message }: SaveRequest) => {
      try {
        const wf = await create.mutateAsync({ document: doc as unknown as Record<string, never>, message });
        toast.success("Workflow created", wf.ref);
        await navigate({ to: "/workflows/$workflowId", params: { workflowId: wf.id }, search: { tab: "editor" } });
        return true;
      } catch {
        return false;
      }
    },
    [create, navigate],
  );
  if (!started) {
    return (
      <form
        className="grid max-w-xl gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!name || nameError) return;
          setStarted(emptyWorkflow(name, ws.current?.project.project_id ?? "default"));
        }}
      >
        <Field label="Workflow name" htmlFor="new-wf-name" error={nameError} hint="References use this name: workflow://<project>/<name>. You cannot change it later.">
          <Input id="new-wf-name" autoFocus className="font-mono" value={name} onChange={(e) => setName(e.target.value.toLowerCase())} placeholder="weekly-readiness" />
        </Field>
        <div className="flex gap-2">
          <Button type="submit" disabled={!name || Boolean(nameError)}>
            <GitBranchPlus /> Open the editor
          </Button>
          <Button variant="outline" asChild>
            <Link to="/workflows" search={{ tab: "templates" }}>
              Start from a template instead
            </Link>
          </Button>
        </div>
      </form>
    );
  }
  if (catalog.isPending) return <LoadingRows rows={6} />;
  return <WorkflowEditor storageKey={`new-${started.metadata.name}`} initial={started} catalog={catalog.map} baseRevision={null} saving={create.isPending} saveError={create.error} onSave={onSave} />;
}

export function NewWorkflowPage() {
  const { template, data } = newRoute.useSearch();
  return (
    <>
      <PageHeader title={template ? "New workflow from a template" : "New workflow"} description={template ? "Give it a name. Then set its defaults and bind your data." : "Give it a name. Then add nodes from the palette."} />
      {template ? <TemplateWizard templateId={template} initialData={data} /> : <BlankWorkflow />}
    </>
  );
}

export function RecipePage() {
  const { template, data } = recipeRoute.useSearch();
  return (
    <>
      <PageHeader title="Run a recipe on your data" description="Select your data and click Run. The app explains each step while it runs." />
      {template ? <RecipeRunner templateId={template} initialData={data} /> : <RecipePicker />}
    </>
  );
}

// ----------------------------------------------------------------------------- detail

function RunsTable({ runs }: { runs: WorkflowRun[] }) {
  const navigate = useNavigate();
  const columns = useMemo<ColumnDef<WorkflowRun, unknown>[]>(
    () => [
      { accessorKey: "status", header: "Status", cell: ({ row }) => <Badge variant={TONE_BADGE[RUN_STATUS_META[row.original.status].tone]}>{RUN_STATUS_META[row.original.status].label}</Badge> },
      {
        accessorKey: "run_id",
        header: "Run",
        cell: ({ row }) => (
          <Link to="/workflow-runs/$runId" params={{ runId: row.original.run_id }} className="hover:underline" onClick={(e) => e.stopPropagation()}>
            <Mono>{shortId(row.original.run_id)}</Mono>
          </Link>
        ),
      },
      { accessorKey: "revision", header: "Revision", cell: ({ row }) => <Mono>r{row.original.revision}</Mono> },
      { accessorKey: "trigger", header: "Started by", cell: ({ row }) => TRIGGER_LABEL[row.original.trigger ?? "manual"] },
      { id: "duration", header: "Duration", cell: ({ row }) => formatDuration(elapsedSeconds(row.original.started_at, row.original.finished_at)) },
      { id: "cost", header: "Cost", cell: ({ row }) => runCostView(row.original.cost)?.text ?? "—" },
      { accessorKey: "created_at", header: "Started", cell: ({ row }) => relativeTime(row.original.started_at ?? row.original.created_at) },
    ],
    [],
  );
  return <DataTable label="Runs" data={runs} columns={columns} getRowId={(r) => r.run_id} onRowClick={(r) => void navigate({ to: "/workflow-runs/$runId", params: { runId: r.run_id } })} empty="This workflow has no runs." />;
}

export function WorkflowPage() {
  const { workflowId } = detailRoute.useParams();
  const search = detailRoute.useSearch();
  const navigate = useNavigate({ from: "/workflows/$workflowId" });
  const ws = useWorkspace();
  const persona = useEffectivePersona();
  const wf = useWorkflow(workflowId);
  const latest = wf.data?.latest_revision;
  const revNo = search.rev ?? latest;
  const rev = useWorkflowRevision(workflowId, revNo);
  const latestRev = useWorkflowRevision(workflowId, latest);
  const catalog = useNodeCatalog();
  const runs = useWorkflowRuns(workflowId);
  const save = useCreateWorkflowRevision(workflowId);
  const update = useUpdateWorkflow(workflowId);
  const [running, setRunning] = useState(false);
  const [dirty, setDirty] = useState(false);
  const tab = search.tab ?? (persona === "guided" ? "runs" : "editor");
  const mayEdit = can(ws.grants, "update", "workflow");
  const mayRun = can(ws.grants, "run", "workflow");
  const initial = useMemo(() => (rev.data ? (rev.data.document as unknown as WorkflowDoc) : null), [rev.data]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const onSave = useCallback(
    async ({ doc, message }: SaveRequest) => {
      try {
        const r = await save.mutateAsync({ document: doc as unknown as Record<string, never>, message, expected_revision: latest ?? null });
        toast.success(`Saved revision ${r.revision}`, r.validation.valid ? "It is ready to run." : "It has problems. Correct them before you run it.");
        void navigate({ search: (s) => ({ ...s, tab: "editor", rev: r.revision }), replace: true });
        return true;
      } catch {
        return false;
      }
    },
    [save, latest, navigate],
  );

  if (wf.isPending) return <LoadingRows rows={6} />;
  if (wf.isError || !wf.data) return <ErrorNotice error={wf.error} title="The app could not load the workflow" />;
  const w = wf.data;
  return (
    <>
      <PageHeader
        title={w.name}
        description={w.description ?? undefined}
        meta={
          <>
            <Mono>{w.ref}</Mono>
            <span>·</span>
            <span>latest r{w.latest_revision}</span>
            {w.template ? <Badge variant="outline">from {w.template}</Badge> : null}
            {w.archived ? <Badge variant="warning">archived</Badge> : null}
          </>
        }
        actions={
          <>
            {mayRun ? (
              <Button onClick={() => setRunning(true)} disabled={!latestRev.data}>
                <Play /> Run workflow
              </Button>
            ) : null}
            {mayEdit ? (
              <Button variant="outline" onClick={() => update.mutate({ archived: !w.archived })}>
                <Archive /> {w.archived ? "Unarchive" : "Archive"}
              </Button>
            ) : null}
          </>
        }
      />
      {latestRev.data && !latestRev.data.validation.valid ? (
        <p role="status" className="mb-4 rounded-md border border-warning/40 bg-warning/8 px-3 py-2 text-[13px]">
          The latest revision has problems and cannot run. Open the editor to see each problem next to its node.
        </p>
      ) : null}
      <Tabs
        value={tab}
        onValueChange={(v) => {
          if (dirty && tab === "editor" && !window.confirm("Leave the editor? Your unsaved changes stay until you load another revision.")) return;
          void navigate({ search: (s) => ({ ...s, tab: v as typeof tab }) });
        }}
      >
        <TabsList>
          <TabsTrigger value="editor">Editor</TabsTrigger>
          <TabsTrigger value="runs">Runs</TabsTrigger>
          <TabsTrigger value="automation">Schedules and triggers</TabsTrigger>
          <TabsTrigger value="revisions">Revisions</TabsTrigger>
        </TabsList>
        <TabsContent value="editor">
          {search.rev && latest && search.rev !== latest ? (
            <p className="mb-3 text-[13px] text-muted-foreground">
              You see revision {search.rev}. When you save, the app makes revision {latest + 1} from it.{" "}
              <Link to="/workflows/$workflowId" params={{ workflowId }} search={{ tab: "editor" }} className="text-primary hover:underline">
                Open the latest
              </Link>
            </p>
          ) : null}
          {initial && catalog.map.size ? (
            <WorkflowEditor
              storageKey={workflowId}
              initial={initial}
              catalog={catalog.map}
              baseRevision={rev.data?.revision ?? null}
              saving={save.isPending}
              saveError={save.error instanceof ApiError || save.error ? save.error : null}
              onSave={onSave}
              readOnly={!mayEdit}
              onDirtyChange={setDirty}
            />
          ) : rev.isError ? (
            <ErrorNotice error={rev.error} title="The app could not load the revision" />
          ) : (
            <LoadingRows rows={6} />
          )}
        </TabsContent>
        <TabsContent value="runs">
          <QueryView query={runs}>
            {(page) =>
              page.items.length ? (
                <RunsTable runs={page.items} />
              ) : (
                <EmptyState
                  icon={<Play />}
                  title="No runs yet"
                  action={
                    mayRun ? (
                      <Button onClick={() => setRunning(true)} disabled={!latestRev.data}>
                        <Play /> Run it now
                      </Button>
                    ) : null
                  }
                >
                  A run binds your data. The run records the result, the decision and the cost of each step.
                </EmptyState>
              )
            }
          </QueryView>
        </TabsContent>
        <TabsContent value="automation">{latestRev.data ? <AutomationPanel workflowId={workflowId} doc={latestRev.data.document as unknown as WorkflowDoc} /> : <LoadingRows rows={3} />}</TabsContent>
        <TabsContent value="revisions">
          <RevisionsPanel workflowId={workflowId} latest={w.latest_revision} />
        </TabsContent>
      </Tabs>
      {running && latestRev.data ? <RunDialog workflowId={workflowId} revision={latestRev.data} onClose={() => setRunning(false)} /> : null}
    </>
  );
}

// ----------------------------------------------------------------------------- run

export function WorkflowRunPage() {
  const { runId } = runRoute.useParams();
  const persona = useEffectivePersona();
  return <WorkflowRunMonitor key={runId} runId={runId} plain={persona === "guided"} />;
}
