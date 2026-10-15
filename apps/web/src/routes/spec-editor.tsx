import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import { CheckCircle2, FlaskConical, GitCompare, Play, Save, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { parse as parseYaml } from "yaml";

import { ApiError, errorMessage, newIdempotencyKey } from "@/api/client";
import {
  useCreateRevision,
  useCreateRun,
  useCreateSpec,
  useDryRun,
  useJsonSchema,
  useSpec,
  useSpecRevision,
  useSpecRevisions,
  validateSpec,
} from "@/api/queries";
import type { Issue, SpecRevision, ValidateResponse } from "@/api/types";
import { ErrorNotice, IssueList, LoadingRows, Mono, PageHeader, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Input, NativeSelect } from "@/components/ui/primitives";
import { DiffView } from "@/features/editor/diff-view";
import { setSpecSchema } from "@/features/editor/monaco-setup";
import { buildPipeline } from "@/features/editor/pipeline";
import { PipelineCards } from "@/features/editor/pipeline-cards";
import { STARTER_SPEC } from "@/features/editor/starter";
import { YamlEditor, type YamlEditorHandle } from "@/features/editor/yaml-editor";
import { env } from "@/env";
import { useDebounced } from "@/hooks/use-debounced";
import { relativeTime, shortId } from "@/lib/format";
import { locateIssues, YamlLocator } from "@/lib/yaml-diagnostics";
import { toast } from "@/stores/toast";

const existingRoute = getRouteApi("/build/$specId");

interface ValidationState {
  response: ValidateResponse | null;
  /** Issues from a 4xx problem (unparseable YAML etc.). */
  problemIssues: Issue[];
  problem: ApiError | null;
}

function useLiveValidation(text: string): { state: ValidationState; validating: boolean; validateNow: () => void } {
  const debounced = useDebounced(text, 450);
  const [nonce, setNonce] = useState(0);
  const q = useQuery({
    queryKey: ["validate", debounced, nonce],
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

function useSchemaBinding() {
  const schema = useJsonSchema("modelspec.json");
  useEffect(() => {
    if (schema.data) void setSpecSchema(`${env.apiBase}/v1/schemas/modelspec.json`, schema.data);
  }, [schema.data]);
  return schema;
}

export function NewSpecPage() {
  return <SpecWorkbench specId={null} revision={undefined} />;
}

export function SpecEditorPage() {
  const { specId } = existingRoute.useParams();
  const { rev } = existingRoute.useSearch();
  return <SpecWorkbench key={specId} specId={specId} revision={rev} />;
}

function SpecWorkbench({ specId, revision }: { specId: string | null; revision: number | undefined }) {
  const navigate = useNavigate();
  const spec = useSpec(specId ?? undefined);
  const revisions = useSpecRevisions(specId ?? undefined);
  const selectedRev = revision ?? spec.data?.latest_revision;
  const loaded = useSpecRevision(specId ?? undefined, selectedRev);
  const schema = useSchemaBinding();

  const [text, setText] = useState(specId ? "" : STARTER_SPEC);
  const [base, setBase] = useState(specId ? "" : "");
  const [message, setMessage] = useState("");
  const [compare, setCompare] = useState<{ a: number; b: number } | null>(null);
  const editor = useRef<YamlEditorHandle | null>(null);
  const loadedKey = useRef<string | null>(null);

  // Load the selected revision into the editor once per (spec, revision).
  useEffect(() => {
    if (!loaded.data) return;
    const key = `${loaded.data.spec_id}@${loaded.data.revision}`;
    if (loadedKey.current === key) return;
    loadedKey.current = key;
    setText(loaded.data.yaml);
    setBase(loaded.data.yaml);
  }, [loaded.data]);

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

  const createSpec = useCreateSpec();
  const createRevision = useCreateRevision(specId ?? "");
  const dryRun = useDryRun();
  const createRun = useCreateRun();
  const fitKey = useRef(newIdempotencyKey());

  const dirty = text !== base;
  const latest = spec.data?.latest_revision;
  const viewingOld = specId !== null && selectedRev !== undefined && latest !== undefined && selectedRev !== latest;
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
    if (latest === undefined) return;
    createRevision.mutate(
      { ...body, parent_revision: latest },
      {
        onSuccess: (r: SpecRevision) => {
          toast.success(`Saved revision ${r.revision}`, r.valid ? "Static validation passed." : "Saved with validation issues.");
          loadedKey.current = `${r.spec_id}@${r.revision}`;
          setBase(text);
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
    const { range } = new YamlLocator(text).locate(issue.pointer ?? "");
    editor.current?.reveal(range);
  };

  const canRun = specId !== null && selectedRev !== undefined && !dirty;

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
            {selectedRev ? <Badge variant="outline">r{selectedRev}{viewingOld ? ` of ${latest}` : ""}</Badge> : null}
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
                selectedRev !== undefined &&
                dryRun.mutate(
                  { specId, revision: selectedRev },
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
                selectedRev !== undefined &&
                createRun.mutate(
                  { key: fitKey.current, body: { kind: "fit", spec_id: specId, revision: selectedRev } },
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
          {compare && specId ? (
            <RevisionDiff specId={specId} a={compare.a} b={compare.b} onClose={() => setCompare(null)} />
          ) : specId && loaded.isPending ? (
            <LoadingRows rows={8} />
          ) : (
            <YamlEditor
              path={`spec-${specId ?? "new"}.mapi.yaml`}
              value={text}
              onChange={setText}
              issues={located}
              onSave={save}
              onValidate={validateNow}
              onReady={(h) => (editor.current = h)}
              ariaLabel="Spec YAML"
              height="calc(100svh - 15rem)"
            />
          )}
          {validation.problem && !validation.response ? <ErrorNotice error={validation.problem} title="The document could not be validated" /> : null}
        </div>

        <aside className="min-w-0">
          <Tabs defaultValue="pipeline">
            <TabsList>
              <TabsTrigger value="pipeline">Pipeline</TabsTrigger>
              <TabsTrigger value="issues">
                Issues {allIssues.length ? <Badge variant={errorCount ? "danger" : "warning"}>{allIssues.length}</Badge> : null}
              </TabsTrigger>
              <TabsTrigger value="revisions" disabled={!specId}>
                Revisions
              </TabsTrigger>
            </TabsList>
            <TabsContent value="pipeline" className="max-h-[calc(100svh-17rem)] overflow-y-auto pr-1">
              <PipelineCards model={pipeline} onIssue={reveal} />
            </TabsContent>
            <TabsContent value="issues">
              {allIssues.length ? (
                <IssueList issues={allIssues} onSelect={reveal} />
              ) : (
                <p className="text-muted-foreground">{validation.response ? "No issues. The spec passes static validation." : "Waiting for validation."}</p>
              )}
            </TabsContent>
            <TabsContent value="revisions">
              {specId ? (
                <RevisionList
                  revisions={revisions.data?.items ?? []}
                  current={selectedRev}
                  onOpen={(r) => {
                    if (dirty && !window.confirm("Discard unsaved changes?")) return;
                    loadedKey.current = null;
                    setCompare(null);
                    void navigate({ to: "/build/$specId", params: { specId }, search: { rev: r } });
                  }}
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
