/**
 * The workflow editor (Expert): node palette, visual canvas, node inspector and the YAML/JSON
 * view of the same document. Every edit - on the canvas, in the inspector or in the text - goes
 * into one draft, which the server validates live; problems are pinned to nodes through their
 * JSON pointers. Saving stores a new revision (`expected_revision` guards concurrent edits).
 */
import { AlertTriangle, CheckCircle2, LayoutGrid, Loader2, Redo2, Save, Undo2 } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { ApiError } from "@/api/client";
import type { Issue } from "@/api/types";
import { ErrorNotice, IssueList, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, SegmentedControl } from "@/components/ui/overlays";
import { Field, Input } from "@/components/ui/primitives";
import { YamlEditor, type YamlEditorHandle } from "@/features/editor/yaml-editor";
import { useHotkeys } from "@/hooks/use-hotkeys";
import { locateIssues } from "@/lib/yaml-diagnostics";
import { cn } from "@/lib/utils";

import type { CanvasEdit } from "./canvas";
import { addNode, connect, disconnect, implId, nodeOfPointer, parseDocText, removeNode, setWhen, toJson, toYaml } from "./doc";
import { NodeInspector } from "./inspector";
import { NodePalette } from "./palette";
import type { Catalog } from "./ports";
import { useSubworkflowIndex } from "./subworkflows";
import type { WorkflowDoc } from "./types";
import { pinIssues, useCanvasPositions, useDocHistory, useLiveValidation } from "./use-editor";
import { WorkflowSettings } from "./workflow-settings";

const WorkflowCanvas = lazy(() => import("./canvas"));

export function CanvasFallback({ height = "70vh" }: { height?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 rounded-lg border text-muted-foreground" style={{ height }}>
      <Spinner /> Loading the canvas…
    </div>
  );
}

export { WorkflowCanvas as LazyWorkflowCanvas };

type View = "canvas" | "yaml" | "json";

export interface SaveRequest {
  doc: WorkflowDoc;
  message: string | null;
}

export function WorkflowEditor({
  storageKey,
  initial,
  catalog,
  baseRevision,
  saving,
  saveError,
  onSave,
  readOnly = false,
  onDirtyChange,
}: {
  /** Workflow id (or "new"): where node positions are remembered. */
  storageKey: string;
  initial: WorkflowDoc;
  catalog: Catalog;
  baseRevision: number | null;
  saving: boolean;
  saveError: unknown;
  onSave: (req: SaveRequest) => Promise<boolean>;
  readOnly?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
}) {

  const history = useDocHistory(initial);
  const { doc } = history;
  const [baseJson, setBaseJson] = useState(() => JSON.stringify(initial));
  const dirty = JSON.stringify(doc) !== baseJson;
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);
  useEffect(() => {
    // A newly loaded revision replaces the draft (the page asks before discarding edits).
    history.reset(initial);
    setBaseJson(JSON.stringify(initial));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial]);

  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<View>("canvas");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [saveOpen, setSaveOpen] = useState(false);
  const [message, setMessage] = useState("");
  const live = useLiveValidation(doc);
  const subs = useSubworkflowIndex(doc, catalog);
  const { positions, move, rename, arrange } = useCanvasPositions(storageKey, doc);
  const issues: Issue[] = useMemo(() => [...(live.validation?.issues ?? []), ...(live.problem?.problem?.errors ?? [])], [live.validation, live.problem]);
  const pinned = useMemo(() => pinIssues(doc, issues), [doc, issues]);
  const canvasIssues = useMemo(() => new Map([...pinned.byNode].map(([k, v]) => [k, v.map((x) => x.issue)])), [pinned]);
  const errors = issues.filter((i) => (i.severity ?? "error") === "error");
  const node = selected ? doc.nodes.find((n) => n.id === selected) : undefined;

  useEffect(() => {
    if (selected && !doc.nodes.some((n) => n.id === selected)) setSelected(null);
  }, [doc, selected]);
  useEffect(() => {
    if (!refusal) return;
    const t = setTimeout(() => setRefusal(null), 6000);
    return () => clearTimeout(t);
  }, [refusal]);

  const change = useCallback((next: WorkflowDoc) => history.set(next), [history]);

  const onEdit = useCallback(
    (edit: CanvasEdit) => {
      switch (edit.type) {
        case "connect":
          change(connect(doc, edit.binding, edit.target, edit.port, edit.mode));
          break;
        case "disconnect":
          change(disconnect(doc, edit.target, edit.port, edit.binding));
          break;
        case "gate":
          change(setWhen(doc, edit.target, edit.when));
          break;
        case "remove-nodes":
          change(edit.ids.reduce((d, id) => removeNode(d, id), doc));
          break;
        case "add-node": {
          const added = addNode(doc, edit.impl);
          move(added.id, edit.position);
          change(added.doc);
          setSelected(added.id);
          break;
        }
      }
    },
    [change, doc, move],
  );

  /** The keyboard path: the new node takes its place in the automatic layout and is selected. */
  const addFromPalette = (impl: string) => {
    const added = addNode(doc, impl);
    change(added.doc);
    setSelected(added.id);
  };

  // ------------------------------------------------------------------ text view
  const [text, setText] = useState(() => toYaml(initial));
  const [textError, setTextError] = useState<string | null>(null);
  const fromText = useRef(false);
  const editorRef = useRef<YamlEditorHandle | null>(null);
  useEffect(() => {
    if (fromText.current) {
      fromText.current = false;
      return;
    }
    setText(view === "json" ? toJson(doc) : toYaml(doc));
    setTextError(null);
  }, [doc, view]);
  const onText = (t: string) => {
    setText(t);
    const parsed = parseDocText(t);
    if (!parsed.ok) {
      setTextError(parsed.error);
      return;
    }
    setTextError(null);
    if (JSON.stringify(parsed.doc) !== JSON.stringify(doc)) {
      fromText.current = true;
      change(parsed.doc);
    }
  };
  const located = useMemo(() => (view === "canvas" ? [] : locateIssues(text, issues)), [view, text, issues]);

  const save = async (e?: FormEvent) => {
    e?.preventDefault();
    const ok = await onSave({ doc, message: message.trim() || null });
    if (ok) {
      setBaseJson(JSON.stringify(doc));
      setSaveOpen(false);
      setMessage("");
    }
  };

  useHotkeys(
    {
      "mod+s": (e) => {
        e.preventDefault();
        if (!readOnly && doc.nodes.length) setSaveOpen(true);
      },
      Escape: () => setSelected(null),
    },
    true,
  );

  const selectIssue = (issue: Issue) => {
    const hit = nodeOfPointer(doc, issue.pointer);
    if (hit) {
      setSelected(hit.id);
      if (view !== "canvas") editorRef.current?.reveal(locateIssues(text, [issue])[0]!.range);
    } else setSelected(null);
  };

  const status = live.validating ? (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <Loader2 className="size-3.5 animate-spin" aria-hidden /> Checking…
    </span>
  ) : doc.nodes.length === 0 ? (
    <span className="text-xs text-muted-foreground">Add a node to start</span>
  ) : errors.length ? (
    <span className="flex items-center gap-1.5 text-xs font-medium text-destructive">
      <AlertTriangle className="size-3.5" aria-hidden /> {errors.length} problem{errors.length === 1 ? "" : "s"}
    </span>
  ) : (
    <span className="flex items-center gap-1.5 text-xs font-medium text-success">
      <CheckCircle2 className="size-3.5" aria-hidden /> Ready to run
    </span>
  );

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-3" data-testid="workflow-editor">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-card px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <SegmentedControl
            label="Editor view"
            value={view}
            onValueChange={setView}
            options={[
              { value: "canvas", label: "Canvas" },
              { value: "yaml", label: "YAML" },
              { value: "json", label: "JSON" },
            ]}
          />
          <Button variant="ghost" size="icon-sm" aria-label="Undo" onClick={history.undo} disabled={!history.canUndo || readOnly}>
            <Undo2 />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="Redo" onClick={history.redo} disabled={!history.canRedo || readOnly}>
            <Redo2 />
          </Button>
          {view === "canvas" ? (
            <Button variant="ghost" size="sm" onClick={arrange}>
              <LayoutGrid /> Tidy layout
            </Button>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-3" role="status" aria-live="polite">
          {status}
          {dirty ? <span className="text-xs text-muted-foreground">Unsaved changes{baseRevision ? ` (on r${baseRevision})` : ""}</span> : null}
          {!readOnly ? (
            <Button size="sm" onClick={() => setSaveOpen(true)} disabled={!dirty || !doc.nodes.length || saving}>
              {saving ? <Spinner /> : <Save />} {baseRevision ? "Save revision" : "Create workflow"}
            </Button>
          ) : null}
        </div>
      </div>

      {refusal ? (
        <p role="alert" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/8 px-3 py-2 text-[13px]">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          <span>
            <span className="font-medium">That connection is not possible. </span>
            {refusal}
          </span>
        </p>
      ) : null}

      {/* Wide screens: palette | canvas | inspector. Otherwise the canvas gets the full width
          and the palette and inspector sit side by side below it. */}
      <div className="grid gap-3 lg:grid-cols-2 2xl:grid-cols-[250px_minmax(0,1fr)_380px]">
        <aside className="order-2 min-w-0 rounded-lg border bg-card p-3 2xl:order-none" aria-label="Add nodes">
          <h2 className="mb-2 text-sm font-semibold">Nodes</h2>
          <NodePalette catalog={catalog} onAdd={addFromPalette} disabled={readOnly} />
        </aside>
        <div className="order-1 min-w-0 lg:col-span-2 2xl:order-none 2xl:col-span-1">
          {view === "canvas" ? (
            <Suspense fallback={<CanvasFallback />}>
              <WorkflowCanvas
                label="Workflow canvas"
                doc={doc}
                catalog={catalog}
                subs={subs}
                positions={positions}
                issues={canvasIssues}
                selected={selected}
                readOnly={readOnly}
                onSelect={setSelected}
                onEdit={onEdit}
                onMove={move}
                onRefused={setRefusal}
              />
            </Suspense>
          ) : (
            <div className="grid gap-2">
              <YamlEditor
                path={`workflow-${storageKey}.${view}`}
                value={text}
                onChange={onText}
                issues={located}
                readOnly={readOnly}
                onSave={() => setSaveOpen(true)}
                onReady={(h) => {
                  editorRef.current = h;
                }}
                height="70vh"
                ariaLabel={`Workflow document (${view.toUpperCase()})`}
              />
              {textError ? (
                <p role="alert" className="text-[13px] text-destructive">
                  The canvas shows the last readable version: {textError}
                </p>
              ) : null}
            </div>
          )}
          {issues.length ? (
            <section aria-label="Problems" className="mt-3 rounded-lg border bg-card p-3">
              <h2 className="mb-1 text-sm font-semibold">Problems</h2>
              <IssueList issues={issues} onSelect={selectIssue} />
            </section>
          ) : null}
          {live.problem && !live.problem.problem?.errors?.length ? <ErrorNotice error={live.problem} title="The document could not be checked" /> : null}
        </div>
        <aside className={cn("order-3 min-w-0 rounded-lg border bg-card p-4 2xl:order-none", "max-h-[calc(70vh+8rem)] overflow-y-auto")} aria-label={node ? `Edit node ${node.id}` : "Workflow settings"}>
          {node ? (
            <NodeInspector
              key={node.id}
              doc={doc}
              node={node}
              catalog={catalog}
              subs={subs}
              issues={pinned.byNode.get(node.id) ?? []}
              validating={live.validating}
              onChange={(next) => {
                const at = doc.nodes.findIndex((n) => n.id === node.id);
                const to = next.nodes[at]?.id;
                if (to && to !== node.id && !next.nodes.some((n) => n.id === node.id)) rename(node.id, to);
                change(next);
              }}
              onSelect={setSelected}
              onRemove={() => {
                change(removeNode(doc, node.id));
                setSelected(null);
              }}
            />
          ) : (
            <WorkflowSettings doc={doc} catalog={catalog} subs={subs} issues={pinned.general} onChange={change} />
          )}
        </aside>
      </div>

      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent>
          <DialogTitle>{baseRevision ? `Save revision ${baseRevision + 1}` : "Create the workflow"}</DialogTitle>
          <DialogDescription>
            {errors.length
              ? `The document has ${errors.length} problem${errors.length === 1 ? "" : "s"}. It is saved so you can fix it later, but it cannot run until a revision without problems is saved.`
              : "The server checks the document again when it is saved."}
          </DialogDescription>
          <form className="grid gap-4" onSubmit={(e) => void save(e)}>
            <Field label="What changed (optional)" htmlFor="wf-save-message">
              <Input id="wf-save-message" autoFocus maxLength={2000} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Smaller AR and MA search" />
            </Field>
            {saveError ? (
              saveError instanceof ApiError && saveError.code === "CONFLICT" ? (
                <p role="alert" className="text-[13px] text-warning">
                  Someone saved a newer revision meanwhile. Your draft is kept: open the Revisions tab to compare, then save again on top of the latest.
                </p>
              ) : (
                <ErrorNotice error={saveError} title="The workflow was not saved" />
              )
            ) : null}
            <DialogFooter>
              <Button variant="ghost" onClick={() => setSaveOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={saving}>
                {saving ? <Spinner /> : <Save />} Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** The node ids in a document whose implementation the catalog does not know. */
export function unknownNodes(doc: WorkflowDoc, catalog: Catalog): string[] {
  return doc.nodes.filter((n) => !catalog.has(implId(n.use))).map((n) => n.id);
}
