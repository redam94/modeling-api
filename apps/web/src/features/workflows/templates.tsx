/**
 * The template gallery (`GET /v1/workflow-templates`, each with its About card), the Expert
 * "use this template" wizard (name → parameters → data → create, optionally run now) and the
 * Guided "run this recipe on my data" flow (data → a few plain settings → run).
 */
import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, ArrowRight, Play, Sparkles, Wand2 } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { ApiError, newIdempotencyKey } from "@/api/client";
import { EmptyState, ErrorNotice, LoadingRows, QueryView, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Badge, Card, CardContent, Field, Input } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { useWorkspace } from "@/features/auth/workspace";
import type { JsonObject } from "@/features/forms/json-schema-form";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { useCreateWorkflow, useStartWorkflowRun, useWorkflows, useWorkflowTemplate, useWorkflowTemplates } from "./api";
import { DatasetRefPicker, InputsForm, missingInputs, missingParams, ParamsForm } from "./run-inputs";
import type { WorkflowDoc, WorkflowTemplate } from "./types";

const NAME_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

export function TemplateGallery({ guided }: { guided: boolean }) {
  const templates = useWorkflowTemplates();
  const ws = useWorkspace();
  const mayCreate = can(ws.grants, "create", "workflow");
  const mayRun = can(ws.grants, "run", "workflow");
  return (
    <QueryView query={templates}>
      {(page) =>
        page.items.length ? (
          <div className="grid gap-4 lg:grid-cols-2" data-testid="template-gallery">
            {[...page.items]
              .sort((a, b) => (guided ? difficultyRank(a) - difficultyRank(b) : 0))
              .map((t) => (
                <AboutCard
                  framed
                  key={t.id}
                  about={t.about}
                  headingLevel={2}
                  heading={
                    <span className="flex flex-wrap items-center gap-2">
                      {t.about.title}
                      {(t.tags ?? []).slice(0, 3).map((tag) => (
                        <Badge key={tag} variant="outline" className="font-normal">
                          {tag}
                        </Badge>
                      ))}
                    </span>
                  }
                  actions={
                    <>
                      {mayRun ? (
                        <Button size="sm" asChild>
                          <Link to="/workflows/recipe" search={{ template: t.id }}>
                            <Play /> Run on my data
                          </Link>
                        </Button>
                      ) : (
                        <Button size="sm" disabled title="You need the runner role to run a workflow.">
                          <Play /> Run on my data
                        </Button>
                      )}
                      {guided || !mayCreate ? null : (
                        <Button size="sm" variant="outline" asChild>
                          <Link to="/workflows/new" search={{ template: t.id }}>
                            <Wand2 /> Use this template
                          </Link>
                        </Button>
                      )}
                    </>
                  }
                />
              ))}
          </div>
        ) : (
          <EmptyState title="No templates">The server has no workflow templates to show.</EmptyState>
        )
      }
    </QueryView>
  );
}

function difficultyRank(t: WorkflowTemplate): number {
  return { beginner: 0, intermediate: 1, expert: 2 }[t.about.difficulty];
}

/** The template's document named `name` in `project`, with parameter defaults and an input default. */
export function instantiate(t: WorkflowTemplate, name: string, project: string, params: JsonObject, inputs: Record<string, string>): WorkflowDoc {
  const doc = structuredClone(t.document) as unknown as WorkflowDoc;
  doc.metadata = { ...doc.metadata, name, project, labels: { ...doc.metadata.labels, "workflow-template": t.id } };
  for (const [k, v] of Object.entries(params)) {
    const p = doc.params?.[k];
    if (!p) continue;
    p.default = v;
    delete p.required;
  }
  for (const [k, ref] of Object.entries(inputs)) {
    const decl = doc.inputs?.[k];
    if (decl && ref) decl.default = ref;
  }
  return doc;
}

const STEPS = ["About", "Parameters", "Data"] as const;

/** Expert: create a workflow from a template, with chosen defaults and a bound dataset. */
/** The first dataset input of a template (the one "your data" binds to). */
function firstDatasetInput(doc: WorkflowDoc | null): string | undefined {
  return Object.entries(doc?.inputs ?? {}).find(([, d]) => d.type === "dataset")?.[0];
}

/** `initialData` (a dataset ref) is bound to the template's data input until changed. */
export function TemplateWizard({ templateId, initialData }: { templateId: string; initialData?: string | undefined }) {
  const navigate = useNavigate();
  const ws = useWorkspace();
  const tpl = useWorkflowTemplate(templateId);
  const workflows = useWorkflows();
  const create = useCreateWorkflow();
  const [step, setStep] = useState(0);
  const [name, setName] = useState<string | null>(null);
  const [params, setParams] = useState<JsonObject>({});
  const [chosenInputs, setInputs] = useState<Record<string, string> | null>(null);
  const [runNow, setRunNow] = useState(true);
  const [busy, setBusy] = useState(false);
  const t = tpl.data;
  const doc = t ? (t.document as unknown as WorkflowDoc) : null;
  const seededInput = initialData ? firstDatasetInput(doc) : undefined;
  const inputs = chosenInputs ?? (seededInput && initialData ? { [seededInput]: initialData } : {});
  const defaultName = doc?.metadata.name ?? "";
  const effectiveName = name ?? defaultName;
  const taken = workflows.data?.items.some((w) => w.name === effectiveName) ?? false;
  const nameError = !NAME_RE.test(effectiveName) ? "The name is not correct. Use lowercase letters, digits and -. Start and end with a letter or a digit." : taken ? "The project already has a workflow with this name. Type a different name." : null;
  const missing = doc ? missingParams(doc.params ?? {}, params) : [];
  const project = ws.current?.project.project_id ?? "default";

  if (tpl.isPending) return <LoadingRows rows={6} />;
  if (tpl.isError || !t || !doc) return <ErrorNotice error={tpl.error} title="The template could not be loaded" />;

  const finish = async (e: FormEvent) => {
    e.preventDefault();
    if (nameError) return setStep(0);
    if (missing.length) return setStep(1);
    setBusy(true);
    try {
      const document = instantiate(t, effectiveName, project, params, inputs);
      const wf = await create.mutateAsync({ document: document as unknown as Record<string, never>, message: `From the gallery template ${t.id}` });
      toast.success("Workflow created", `${wf.ref} (revision 1)`);
      if (runNow && !missingInputs(document.inputs ?? {}, inputs).length) {
        // The run binds the same data; parameters come from the defaults just saved.
        await navigate({ to: "/workflows/$workflowId", params: { workflowId: wf.id }, search: { tab: "runs" } });
      } else await navigate({ to: "/workflows/$workflowId", params: { workflowId: wf.id }, search: { tab: "editor" } });
    } catch {
      // shown below
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="grid gap-5" onSubmit={(e) => void finish(e)} noValidate>
      <ol className="flex flex-wrap gap-2" aria-label="Steps">
        {STEPS.map((s, i) => (
          <li key={s}>
            <button
              type="button"
              onClick={() => setStep(i)}
              aria-current={step === i ? "step" : undefined}
              className={cn("rounded-full border px-3 py-1 text-xs font-medium", step === i ? "border-primary bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent")}
            >
              {i + 1}. {s}
            </button>
          </li>
        ))}
      </ol>
      {step === 0 ? (
        <div className="grid gap-4">
          <AboutCard framed about={t.about} headingLevel={2} />
          <Field label="Workflow name" htmlFor="wf-name" error={nameError} hint="References use this name: workflow://<project>/<name>. You cannot change it later.">
            <Input id="wf-name" className="font-mono" value={effectiveName} onChange={(e) => setName(e.target.value.toLowerCase())} />
          </Field>
        </div>
      ) : step === 1 ? (
        <Card>
          <CardContent className="grid gap-3 pt-4">
            <p className="text-[13px] text-muted-foreground">These values become the defaults of the workflow. Each run can change them.</p>
            <ParamsForm doc={doc} value={params} onChange={setParams} />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="grid gap-4 pt-4">
            <p className="text-[13px] text-muted-foreground">Select the default data for the workflow. Schedules use this data. Runs that start with no data also use it.</p>
            <InputsForm inputs={doc.inputs ?? {}} value={inputs} onChange={setInputs} />
            <label className="flex items-center gap-2 text-[13px]">
              <Checkbox checked={runNow} onCheckedChange={(c) => setRunNow(c === true)} />
              Open the Runs tab to start a run immediately
            </label>
          </CardContent>
        </Card>
      )}
      {create.isError ? <ErrorNotice error={create.error} title="The workflow was not created" /> : null}
      <div className="flex flex-wrap justify-between gap-2">
        <Button variant="ghost" onClick={() => (step === 0 ? void navigate({ to: "/workflows", search: { tab: "templates" } }) : setStep(step - 1))}>
          <ArrowLeft /> {step === 0 ? "Gallery" : "Back"}
        </Button>
        {step < STEPS.length - 1 ? (
          <Button onClick={() => setStep(step + 1)} disabled={step === 0 && Boolean(nameError)}>
            Next <ArrowRight />
          </Button>
        ) : (
          <Button type="submit" disabled={busy}>
            {busy ? <Spinner /> : <Sparkles />} Create workflow
          </Button>
        )}
      </div>
    </form>
  );
}

/**
 * Guided: run a recipe (a gallery template) on the user's data. The workflow is created from the
 * template the first time (or reused when the project already has it); the run carries the data
 * and the settings, so nothing else has to be edited.
 */
export function RecipeRunner({ templateId, initialData }: { templateId: string; initialData?: string | undefined }) {
  const navigate = useNavigate();
  const tpl = useWorkflowTemplate(templateId);
  const workflows = useWorkflows();
  const create = useCreateWorkflow();
  const [data, setData] = useState(initialData ?? "");
  const [params, setParams] = useState<JsonObject>({});
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const existing = useMemo(() => workflows.data?.items.find((w) => w.template === templateId && !w.archived), [workflows.data, templateId]);
  const [wfId, setWfId] = useState<string | null>(null);
  const start = useStartWorkflowRun(wfId ?? existing?.id ?? "");
  const key = useMemo(() => newIdempotencyKey(), []);
  const t = tpl.data;
  const doc = t ? (t.document as unknown as WorkflowDoc) : null;
  if (tpl.isPending || workflows.isPending) return <LoadingRows rows={5} />;
  if (tpl.isError || !t || !doc) return <ErrorNotice error={tpl.error} title="The recipe could not be loaded" />;
  const dataInput = Object.entries(doc.inputs ?? {}).find(([, d]) => d.type === "dataset")?.[0] ?? "data";
  const requiredParams = Object.fromEntries(Object.entries(doc.params ?? {}).filter(([, p]) => p.required));
  const optionalParams = Object.fromEntries(Object.entries(doc.params ?? {}).filter(([, p]) => !p.required));
  const missing = [...(data ? [] : ["your data"]), ...missingParams(doc.params ?? {}, params)];

  const run = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (missing.length) return;
    setBusy(true);
    setError(null);
    try {
      let id = wfId ?? existing?.id ?? null;
      if (!id) {
        try {
          id = (await create.mutateAsync({ template: t.id, message: `From the gallery template ${t.id}` })).id;
        } catch (err) {
          if (!(err instanceof ApiError && err.code === "CONFLICT")) throw err;
          const name = `${doc.metadata.name}-${new Date().toISOString().slice(0, 10)}`.replace(/[^a-z0-9-]/g, "-");
          id = (await create.mutateAsync({ document: instantiate(t, name, doc.metadata.project, {}, {}) as unknown as Record<string, never>, message: `From the gallery template ${t.id}` })).id;
        }
        setWfId(id);
      }
      const res = await start.mutateAsync({ body: { params, inputs: { [dataInput]: data }, use_cache: true }, key, workflowId: id });
      await navigate({ to: "/workflow-runs/$runId", params: { runId: res.data.run_id } });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="grid gap-5" onSubmit={(e) => void run(e)} noValidate data-testid="recipe-runner">
      <AboutCard framed about={t.about} headingLevel={2} />
      <Card>
        <CardContent className="grid gap-5 pt-4">
          <section className="grid gap-2" aria-labelledby="recipe-data">
            <h3 id="recipe-data" className="text-sm font-semibold">
              1. Your data
            </h3>
            <DatasetRefPicker value={data} onChange={setData} label="Dataset" hint={doc.inputs?.[dataInput]?.description ?? undefined} />
          </section>
          {Object.keys(requiredParams).length ? (
            <section className="grid gap-2" aria-labelledby="recipe-needed">
              <h3 id="recipe-needed" className="text-sm font-semibold">
                2. What the recipe needs to know
              </h3>
              <ParamsForm doc={{ params: requiredParams }} value={params} onChange={(v) => setParams({ ...params, ...v })} />
            </section>
          ) : null}
          {Object.keys(optionalParams).length ? (
            <details className="rounded-md border p-3">
              <summary className="cursor-pointer text-sm font-semibold">Fine-tune (optional: the defaults are correct for most data)</summary>
              <div className="mt-3">
                <ParamsForm
                  doc={{ params: optionalParams }}
                  value={Object.fromEntries(Object.entries(params).filter(([k]) => k in optionalParams))}
                  onChange={(v) => setParams({ ...Object.fromEntries(Object.entries(params).filter(([k]) => !(k in optionalParams))), ...v })}
                />
              </div>
            </details>
          ) : null}
          {existing ? (
            <p className="text-xs text-muted-foreground">
              This recipe runs as the workflow <span className="font-mono">{existing.name}</span> of your project.
            </p>
          ) : null}
          {submitted && missing.length ? (
            <p role="alert" className="text-[13px] text-destructive">
              Still needed: {missing.join(", ")}. Give a value for each item.
            </p>
          ) : null}
          {error ? <ErrorNotice error={error} title="The recipe did not start" /> : null}
          <div className="flex justify-end">
            <Button type="submit" size="lg" disabled={busy}>
              {busy ? <Spinner /> : <Play />} Run this recipe
            </Button>
          </div>
        </CardContent>
      </Card>
    </form>
  );
}

/** Pick a recipe (Guided), when the URL did not name one. */
export function RecipePicker() {
  const templates = useWorkflowTemplates();
  return (
    <QueryView query={templates}>
      {(page) => (
        <ul className="grid gap-3 md:grid-cols-2">
          {page.items.map((t) => (
            <li key={t.id}>
              <AboutCard
                framed
                about={t.about}
                compact
                headingLevel={2}
                actions={
                  <Button size="sm" asChild>
                    <Link to="/workflows/recipe" search={{ template: t.id }}>
                      Choose <ArrowRight />
                    </Link>
                  </Button>
                }
              />
            </li>
          ))}
        </ul>
      )}
    </QueryView>
  );
}

