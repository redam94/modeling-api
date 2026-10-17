import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import { Blocks, CheckCircle2, Code2, FlaskConical, GitCompare, History, Play, Plus, Save, ShieldCheck, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { parse as parseYaml } from "yaml";

import { ApiError, errorMessage, newIdempotencyKey } from "@/api/client";
import {
  useCreateRevision,
  useCreateRun,
  useCatalog,
  useCreateSpec,
  useDryRun,
  useJsonSchema,
  useSpec,
  useSpecRevision,
  useSpecRevisions,
  qk,
  useProjectScope,
  validateSpec,
} from "@/api/queries";
import type { Issue, SpecRevision, ValidateResponse } from "@/api/types";
import { ErrorNotice, IssueList, LoadingRows, Mono, PageHeader, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { SegmentedControl, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Input, NativeSelect } from "@/components/ui/primitives";
import { useAbout } from "@/features/about/api";
import { SpecAboutPanel } from "@/features/about/spec-about";
import { specStarterFor } from "@/features/about/spec-starter";
import { DiffView } from "@/features/editor/diff-view";
import { InsertPanel } from "@/features/editor/insert-panel";
import { buildPipeline } from "@/features/editor/pipeline";
import { PipelineCards } from "@/features/editor/pipeline-cards";
import { saveParent, shouldLoadRevision } from "@/features/editor/revision-sync";
import { STARTER_SPEC } from "@/features/editor/starter";
import { useHintSources, useSchemaHints } from "@/features/editor/use-spec-hints";
import { YamlEditor, type YamlEditorHandle } from "@/features/editor/yaml-editor";
import { GuidedReturnLink } from "@/features/guided/return-link";
import { VisualBuilder } from "@/features/spec-builder/visual-builder";
import { env } from "@/env";
import { useDebounced } from "@/hooks/use-debounced";
import { relativeTime, shortId } from "@/lib/format";
import { locateIssues, YamlLocator } from "@/lib/yaml-diagnostics";
import { toast } from "@/stores/toast";

const existingRoute = getRouteApi("/build/$specId");

type EditorView = "yaml" | "visual";
const VIEW_KEY = "mapi.spec-editor.view";

/** The last view the user picked (a per-browser convenience; YAML when storage is unavailable). */
function useEditorView(): [EditorView, (v: EditorView) => void] {
  const [view, setView] = useState<EditorView>(() => {
    try {
      return window.localStorage.getItem(VIEW_KEY) === "visual" ? "visual" : "yaml";
    } catch {
      return "yaml";
    }
  });
  const set = useCallback((v: EditorView) => {
    setView(v);
    try {
      window.localStorage.setItem(VIEW_KEY, v);
    } catch {
      // Storage blocked: the choice lasts for this page only.
    }
  }, []);
  return [view, set];
}
const newRoute = getRouteApi("/build/new");

interface ValidationState {
  response: ValidateResponse | null;
  /** Issues from a 4xx problem (unparseable YAML etc.). */
  problemIssues: Issue[];
  problem: ApiError | null;
}

function useLiveValidation(text: string): { state: ValidationState; validating: boolean; validateNow: () => void } {
  const debounced = useDebounced(text, 450);
  const [nonce, setNonce] = useState(0);
  const scope = useProjectScope();
  const q = useQuery({
    queryKey: [...qk.project(scope.key), "validate", debounced, nonce],
    queryFn: async ({ signal }): Promise<ValidationState> => {
      try {
        return { response: await validateSpec(debounced, signal), problemIssues: [], problem: null };
      } catch (err) {
        if (err instanceof ApiError && err.status < 500) {
          return { response: null, problemIssues: err.problem?.errors ?? [], problem: err };
        }
        throw err;
      }
    },
    enabled: debounced.trim().length > 0,
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    retry: 1,
  });
  return {
    state: q.data ?? { response: null, problemIssues: [], problem: null },
    validating: q.isFetching,
    validateNow: () => setNonce((n) => n + 1),
  };
}


export function NewSpecPage() {
  const { model } = newRoute.useSearch();
  const catalog = useCatalog();
  const { about, isPending } = useAbout("model", model ?? null);
  if (!model) return <SpecWorkbench specId={null} revision={undefined} />;
  // "Start with this model" (Help me choose, the catalog): a starter for that model.
  if (isPending || catalog.isPending) return <LoadingRows rows={8} />;
  const version = catalog.data?.models?.filter((m) => m.manifest.id === model).map((m) => m.manifest.version).sort().at(-1);
  return <SpecWorkbench key={model} specId={null} revision={undefined} starter={specStarterFor(model, { version: version ?? null, about: about ?? null })} />;
}

export function SpecEditorPage() {
  const { specId } = existingRoute.useParams();
  const { rev } = existingRoute.useSearch();
  return <SpecWorkbench key={specId} specId={specId} revision={rev} />;
}

function SpecWorkbench({ specId, revision, starter = STARTER_SPEC }: { specId: string | null; revision: number | undefined; starter?: string }) {
  const navigate = useNavigate();
  const spec = useSpec(specId ?? undefined);
  const revisions = useSpecRevisions(specId ?? undefined);
  const selectedRev = revision ?? spec.data?.latest_revision;
  const loaded = useSpecRevision(specId ?? undefined, selectedRev);
  const schema = useJsonSchema("modelspec.json");

  const [text, setText] = useState(specId ? "" : starter);
  const [base, setBase] = useState(specId ? "" : "");
  /** The revision `base` (and so the editor text) came from; null until one is loaded. */
  const [baseRev, setBaseRev] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [compare, setCompare] = useState<{ a: number; b: number } | null>(null);
  const editor = useRef<YamlEditorHandle | null>(null);
  const [view, setView] = useEditorView();
  /** An issue to reveal once the YAML editor is mounted (picked in the visual view). */
  const pendingReveal = useRef<Issue | null>(null);
  const loadedKey = useRef<string | null>(null);
  const dirty = text !== base;
  const latest = spec.data?.latest_revision;

  // Pin the revision being edited into the URL once the latest is known, so a later refetch
  // that sees a newer latest revision cannot change what the editor shows.
  useEffect(() => {
    if (specId && revision === undefined && latest !== undefined) {
      void navigate({ to: "/build/$specId", params: { specId }, search: { rev: latest }, replace: true });
    }
  }, [specId, revision, latest, navigate]);

  // Load the selected revision into the editor once per (spec, revision), and never over
  // unsaved edits unless the user asked for that revision explicitly.
  useEffect(() => {
    if (!loaded.data) return;
    const key = `${loaded.data.spec_id}@${loaded.data.revision}`;
    if (!shouldLoadRevision(key, loadedKey.current, dirty)) return;
    loadedKey.current = key;
    setText(loaded.data.yaml);
    setBase(loaded.data.yaml);
    setBaseRev(loaded.data.revision);
  }, [loaded.data, dirty]);

  /** Load revision `r` on request, discarding unsaved edits only after the user agrees. */
  const openRevision = (r: number) => {
    if (!specId) return;
    if (dirty && !window.confirm(`Discard your unsaved changes and load r${r}?`)) return;
    loadedKey.current = null;
    setCompare(null);
    if (r === loaded.data?.revision) {
      loadedKey.current = `${loaded.data.spec_id}@${r}`;
      setText(loaded.data.yaml);
      setBase(loaded.data.yaml);
      setBaseRev(r);
      return;
    }
    void navigate({ to: "/build/$specId", params: { specId }, search: { rev: r } });
  };

  const { state: validation, validating, validateNow } = useLiveValidation(text);
  const allIssues = useMemo(
    () => [...(validation.response?.issues ?? []), ...validation.problemIssues],
    [validation],
  );
  const located = useMemo(() => locateIssues(text, allIssues), [text, allIssues]);
  const document = useMemo(() => {
    try {
      return parseYaml(text, { maxAliasCount: 0 }) as unknown;
    } catch {
      return null;
    }
  }, [text]);
  const pipeline = useMemo(() => buildPipeline(document, validation.response), [document, validation.response]);
  // Completions and the Insert panel: the schema enriched with this tenant's catalog and data.
  const hints = useHintSources(document, validation.response?.stages);
  useSchemaHints(`${env.apiBase}/v1/schemas/modelspec.json`, schema.data, hints.shape, hints.sources);
  const [panel, setPanel] = useState("pipeline");
  const insertSearch = useRef<HTMLInputElement>(null);
  const insertAtCursor = useCallback((t: string) => editor.current?.insert(t), []);
  const openInsert = () => {
    setPanel("insert");
    requestAnimationFrame(() => insertSearch.current?.focus());
  };

  const createSpec = useCreateSpec();
  const createRevision = useCreateRevision(specId ?? "");
  const dryRun = useDryRun();
  const createRun = useCreateRun();
  const fitKey = useRef(newIdempotencyKey());

  const editingRev = baseRev ?? selectedRev;
  const viewingOld = specId !== null && editingRev !== undefined && latest !== undefined && editingRev < latest;
  // Selected in the URL (history navigation) while the editor still holds edits of another one.
  const heldBack = dirty && loaded.data !== undefined && baseRev !== null && loaded.data.revision !== baseRev;
  const valid = validation.response?.valid ?? false;
  const errorCount = allIssues.filter((i) => (i.severity ?? "error") === "error").length;
  const saving = createSpec.isPending || createRevision.isPending;

  const save = () => {
    if (saving || !text.trim()) return;
    const body = { yaml: text, ...(message.trim() ? { message: message.trim() } : {}) };
    if (specId === null) {
      createSpec.mutate(body, {
        onSuccess: (doc) => {
          toast.success("Spec saved", `${doc.ref} revision 1`);
          setBase(text);
          void navigate({ to: "/build/$specId", params: { specId: doc.id } });
        },
        onError: (e) => toast.error("Save failed", errorMessage(e)),
      });
      return;
    }
    if (baseRev === null) return;
    // Send the revision this text is based on, so a concurrent save elsewhere is a CONFLICT.
    const { parent, overwrites } = saveParent(baseRev, latest);
    if (
      overwrites !== null &&
      !window.confirm(
        `Restore r${baseRev} as a new revision?\n\nYou are editing r${baseRev}, but the latest revision is r${overwrites}. ` +
          `Saving makes r${overwrites + 1} from this text, without the changes made in r${baseRev + 1}–r${overwrites}.`,
      )
    ) {
      return;
    }
    createRevision.mutate(
      { ...body, parent_revision: parent },
      {
        onSuccess: (r: SpecRevision) => {
          toast.success(`Saved revision ${r.revision}`, r.valid ? "Static validation passed." : "Saved with validation issues.");
          loadedKey.current = `${r.spec_id}@${r.revision}`;
          setBase(text);
          setBaseRev(r.revision);
          setMessage("");
          void navigate({ to: "/build/$specId", params: { specId: r.spec_id }, search: { rev: r.revision } });
        },
        onError: (e) =>
          toast.error(
            e instanceof ApiError && e.code === "CONFLICT" ? "Someone saved a newer revision" : "Save failed",
            e instanceof ApiError && e.code === "CONFLICT" ? "Reload the latest revision and re-apply your change." : errorMessage(e),
          ),
      },
    );
  };

  const reveal = (issue: Issue) => {
    if (view !== "yaml" || !editor.current) {
      pendingReveal.current = issue;
      setView("yaml");
      return;
    }
    const { range } = new YamlLocator(text).locate(issue.pointer ?? "");
    editor.current.reveal(range);
  };
  const onEditorReady = (h: YamlEditorHandle) => {
    editor.current = h;
    const issue = pendingReveal.current;
    pendingReveal.current = null;
    if (issue) h.reveal(new YamlLocator(text).locate(issue.pointer ?? "").range);
  };

  // Runs use the revision whose text is on screen, and only when it is saved as shown.
  const runRev = !dirty && baseRev !== null ? baseRev : undefined;
  const canRun = specId !== null && runRev !== undefined;

  if (specId && (spec.isError || loaded.isError)) {
    return <ErrorNotice error={spec.error ?? loaded.error} title="This spec could not be loaded" />;
  }

  return (
    <>
      <PageHeader
        title={spec.data?.name ?? (specId ? <Spinner /> : "New spec")}
        meta={
          <>
            {spec.data ? <Mono>{spec.data.ref}</Mono> : <span>Not saved yet</span>}
            {editingRev ? <Badge variant="outline">r{editingRev}{viewingOld ? ` of ${latest}` : ""}</Badge> : null}
            {dirty ? <Badge variant="warning">unsaved changes</Badge> : null}
            {validating ? (
              <span className="inline-flex items-center gap-1"><Spinner className="size-3" /> validating</span>
            ) : validation.response ? (
              valid ? (
                <span className="inline-flex items-center gap-1 text-success"><CheckCircle2 className="size-3.5" /> valid</span>
              ) : (
                <span className="inline-flex items-center gap-1 text-destructive"><XCircle className="size-3.5" /> {errorCount} error{errorCount === 1 ? "" : "s"}</span>
              )
            ) : validation.problem ? (
              <span className="inline-flex items-center gap-1 text-destructive"><XCircle className="size-3.5" /> {validation.problem.code ?? "invalid"}</span>
            ) : null}
            {schema.isError ? <span className="text-warning">Schema hints unavailable</span> : null}
            <GuidedReturnLink specId={specId} document={document} />
          </>
        }
        actions={
          <>
            <Input
              aria-label="Revision message"
              placeholder="Revision message"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              className="h-9 w-56"
            />
            <Button variant="outline" onClick={openInsert} disabled={compare !== null || view !== "yaml"} title="Search models, steps, datasets and columns to insert">
              <Plus /> Insert
            </Button>
            <Button variant="outline" onClick={validateNow} title="Validate now (⌘Enter)">
              <ShieldCheck /> Validate
            </Button>
            <Button variant="outline" onClick={save} disabled={saving || (!dirty && specId !== null)} title="Save revision (⌘S)">
              <Save /> {specId ? "Save revision" : "Save spec"}
            </Button>
            <Button
              variant="outline"
              disabled={!canRun || dryRun.isPending}
              title={canRun ? "Run a sandboxed dry-run of this revision" : "Save first"}
              onClick={() =>
                specId &&
                runRev !== undefined &&
                dryRun.mutate(
                  { specId, revision: runRev },
                  {
                    onSuccess: (r) => void navigate({ to: "/runs/$runId", params: { runId: r.id } }),
                    onError: (e) => toast.error("Dry-run failed to start", errorMessage(e)),
                  },
                )
              }
            >
              <FlaskConical /> Dry-run
            </Button>
            <Button
              disabled={!canRun || !valid || createRun.isPending}
              title={!canRun ? "Save first" : !valid ? "Fix the errors first" : "Fit this revision"}
              onClick={() =>
                specId &&
                runRev !== undefined &&
                createRun.mutate(
                  { key: fitKey.current, body: { kind: "fit", spec_id: specId, revision: runRev } },
                  {
                    onSuccess: (r) => {
                      fitKey.current = newIdempotencyKey();
                      void navigate({ to: "/runs/$runId", params: { runId: r.id } });
                    },
                    onError: (e) => toast.error("Fit failed to start", errorMessage(e)),
                  },
                )
              }
            >
              <Play /> Fit
            </Button>
          </>
        }
      />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="grid min-w-0 content-start gap-3">
          {!compare ? (
            <SegmentedControl
              label="Editor view"
              value={view}
              onValueChange={(v) => {
                if (v === "visual") editor.current = null;
                setView(v);
              }}
              options={[
                { value: "visual", label: <><Blocks /> Visual</>, title: "Build the spec from blocks: drag steps, models and datasets in" },
                { value: "yaml", label: <><Code2 /> YAML</>, title: "Edit the YAML text" },
              ]}
              className="justify-self-start"
            />
          ) : null}
          {specId && !compare && heldBack && loaded.data ? (
            <RevisionBanner
              text={`r${loaded.data.revision} is selected, but the editor still holds your unsaved changes to r${baseRev}.`}
              action={`Load r${loaded.data.revision}`}
              onAction={() => loaded.data && openRevision(loaded.data.revision)}
            />
          ) : specId && !compare && viewingOld && latest !== undefined && baseRev !== null ? (
            <RevisionBanner
              text={
                dirty
                  ? `A newer revision r${latest} exists. Your unsaved changes are based on r${baseRev}; keep editing, or load r${latest}.`
                  : `You are viewing r${baseRev}. The latest revision is r${latest}.`
              }
              action={`Load r${latest}`}
              onAction={() => openRevision(latest)}
            />
          ) : null}
          {compare && specId ? (
            <RevisionDiff specId={specId} a={compare.a} b={compare.b} onClose={() => setCompare(null)} />
          ) : specId && loaded.isPending ? (
            <LoadingRows rows={8} />
          ) : view === "visual" ? (
            <VisualBuilder
              text={text}
              document={document}
              onText={setText}
              schema={schema.data}
              shape={hints.shape}
              sources={hints.sources}
              issues={allIssues}
              onIssue={reveal}
            />
          ) : (
            <YamlEditor
              path={`spec-${specId ?? "new"}.mapi.yaml`}
              value={text}
              onChange={setText}
              issues={located}
              onSave={save}
              onValidate={validateNow}
              onReady={onEditorReady}
              ariaLabel="Spec YAML"
              height="calc(100svh - 15rem)"
            />
          )}
          {validation.problem && !validation.response ? <ErrorNotice error={validation.problem} title="The document could not be validated" /> : null}
        </div>

        <aside className="min-w-0">
          <Tabs value={panel} onValueChange={setPanel}>
            <TabsList>
              <TabsTrigger value="pipeline">Pipeline</TabsTrigger>
              <TabsTrigger value="insert">Insert</TabsTrigger>
              <TabsTrigger value="issues">
                Issues {allIssues.length ? <Badge variant={errorCount ? "danger" : "warning"}>{allIssues.length}</Badge> : null}
              </TabsTrigger>
              <TabsTrigger value="revisions" disabled={!specId}>
                Revisions
              </TabsTrigger>
              <TabsTrigger value="about">About</TabsTrigger>
            </TabsList>
            <TabsContent value="pipeline" className="max-h-[calc(100svh-17rem)] overflow-y-auto pr-1">
              <PipelineCards model={pipeline} onIssue={reveal} />
            </TabsContent>
            <TabsContent value="insert" className="max-h-[calc(100svh-17rem)] overflow-y-auto pr-1">
              <InsertPanel
                ref={insertSearch}
                text={text}
                shape={hints.shape}
                sources={hints.sources}
                onText={setText}
                onInsertAtCursor={insertAtCursor}
                readOnly={compare !== null}
              />
            </TabsContent>
            <TabsContent value="issues">
              {allIssues.length ? (
                <IssueList issues={allIssues} onSelect={reveal} />
              ) : (
                <p className="text-muted-foreground">{validation.response ? "No issues. The spec passes static validation." : "Waiting for validation."}</p>
              )}
            </TabsContent>
            <TabsContent value="about" className="max-h-[calc(100svh-17rem)] overflow-y-auto pr-1">
              <SpecAboutPanel modelUse={pipeline.modelUse} steps={pipeline.steps.map((s) => ({ id: s.id, use: s.use }))} />
            </TabsContent>
            <TabsContent value="revisions">
              {specId ? (
                <RevisionList
                  revisions={revisions.data?.items ?? []}
                  current={editingRev}
                  onOpen={openRevision}
                  onCompare={(a, b) => setCompare({ a, b })}
                />
              ) : null}
            </TabsContent>
          </Tabs>
        </aside>
      </div>
    </>
  );
}

function RevisionBanner({ text, action, onAction }: { text: string; action: string; onAction: () => void }) {
  return (
    <div role="status" className="flex flex-wrap items-center gap-2 rounded-lg border border-warning/40 bg-warning/8 px-3 py-2 text-[13px]">
      <History className="size-4 shrink-0 text-warning" aria-hidden />
      <span className="min-w-0 flex-1">{text}</span>
      <Button size="sm" variant="outline" onClick={onAction}>
        {action}
      </Button>
    </div>
  );
}

function RevisionList({
  revisions,
  current,
  onOpen,
  onCompare,
}: {
  revisions: SpecRevision[];
  current: number | undefined;
  onOpen: (r: number) => void;
  onCompare: (a: number, b: number) => void;
}) {
  const sorted = [...revisions].sort((a, b) => b.revision - a.revision);
  const [a, setA] = useState<number | null>(sorted[1]?.revision ?? null);
  const [b, setB] = useState<number | null>(sorted[0]?.revision ?? null);
  return (
    <div className="grid gap-4">
      <ol className="grid gap-1">
        {sorted.map((r) => (
          <li key={r.revision}>
            <button
              type="button"
              onClick={() => onOpen(r.revision)}
              aria-current={r.revision === current ? "true" : undefined}
              className="grid w-full grid-cols-[3rem_1fr_auto] items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-accent aria-[current=true]:bg-accent"
            >
              <Mono className="font-semibold">r{r.revision}</Mono>
              <span className="truncate">{r.message ?? <span className="text-muted-foreground">No message</span>}</span>
              <span className="flex items-center gap-2 text-xs text-muted-foreground">
                {r.valid ? <CheckCircle2 className="size-3.5 text-success" /> : <XCircle className="size-3.5 text-destructive" />}
                {relativeTime(r.created_at)}
              </span>
            </button>
          </li>
        ))}
      </ol>
      {sorted.length > 1 ? (
        <div className="flex flex-wrap items-end gap-2 rounded-lg border p-3">
          <GitCompare className="mb-2 size-4 text-muted-foreground" />
          <NativeSelect aria-label="Base revision" value={a ?? ""} onChange={(e) => setA(Number(e.target.value))} className="h-8 w-20">
            {sorted.map((r) => <option key={r.revision} value={r.revision}>r{r.revision}</option>)}
          </NativeSelect>
          <span className="mb-2 text-xs text-muted-foreground">to</span>
          <NativeSelect aria-label="Compared revision" value={b ?? ""} onChange={(e) => setB(Number(e.target.value))} className="h-8 w-20">
            {sorted.map((r) => <option key={r.revision} value={r.revision}>r{r.revision}</option>)}
          </NativeSelect>
          <Button size="sm" variant="outline" disabled={a === null || b === null || a === b} onClick={() => a !== null && b !== null && onCompare(a, b)}>
            Compare
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function RevisionDiff({ specId, a, b, onClose }: { specId: string; a: number; b: number; onClose: () => void }) {
  const ra = useSpecRevision(specId, a);
  const rb = useSpecRevision(specId, b);
  return (
    <div className="grid gap-2">
      <div className="flex items-center gap-2 text-[13px]">
        <GitCompare className="size-4 text-muted-foreground" />
        Comparing <Mono>r{a}</Mono> to <Mono>r{b}</Mono>
        {ra.data && rb.data ? <span className="text-xs text-muted-foreground">hashes {shortId(ra.data.hash)} → {shortId(rb.data.hash)}</span> : null}
        <Button size="sm" variant="ghost" className="ml-auto" onClick={onClose}>
          Back to editor
        </Button>
      </div>
      {ra.data && rb.data ? <DiffView original={ra.data.yaml} modified={rb.data.yaml} height="calc(100svh - 17rem)" /> : <LoadingRows rows={6} />}
    </div>
  );
}
