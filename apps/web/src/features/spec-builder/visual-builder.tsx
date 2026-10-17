/**
 * The visual spec builder: the same ModelSpec document as the YAML view, as a stack of blocks
 * (metadata → data → pipeline steps → model → outputs → execution). Items are dragged in from
 * the palette (or added with its Add button), steps are reordered by dragging their handle (or
 * with the arrow buttons), and each block opens into a form generated from its JSON Schema.
 *
 * Every edit is a structured edit of the YAML text (`features/editor/spec-doc.ts`), so the two
 * views never disagree and comments survive.
 */
import { ArrowDown, ArrowUp, ChevronRight, GripVertical, Trash2 } from "lucide-react";
import { useEffect, useId, useState, type DragEvent, type ReactNode } from "react";

import type { Issue } from "@/api/types";
import { IssueList } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Field, Input, NativeSelect } from "@/components/ui/primitives";
import { SchemaForm, type JsonObject } from "@/features/forms/json-schema-form";
import { addToList, insertStep, moveStep, removeStep, renameStep, setDataset, setModel, setPath, starterWith } from "@/features/editor/spec-doc";
import {
  findModel,
  findStep,
  latestModels,
  namesBeforeStep,
  namesForModel,
  namesOf,
  pinnedUse,
  pluginId,
  plotSnippets,
  templateNames,
  type HintSources,
  type SpecShape,
} from "@/features/editor/spec-hints";
import { pointerWithin } from "@/lib/pointer";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { carries, MOVE_MIME, readDrop, type PaletteItem, type PaletteKind } from "./dnd";
import { KIND_COLOR, SpecPalette } from "./palette";
import { defOf, inlineRefs, omitProps, splitKinds } from "./schema-utils";

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const asObj = (v: unknown): JsonObject => (isObj(v) ? (v as JsonObject) : {});
const STEP_ID = /^[a-z_][a-z0-9_]{0,62}$/;

export interface VisualBuilderProps {
  text: string;
  /** The parsed document (`null` while the YAML does not parse). */
  document: unknown;
  onText: (text: string) => void;
  /** The server's ModelSpec JSON Schema (block forms). */
  schema: unknown;
  shape: SpecShape;
  sources: HintSources;
  issues: Issue[];
  /** Show an issue in the YAML view. */
  onIssue: (issue: Issue) => void;
  readOnly?: boolean | undefined;
}

/** Field key → message, for issues at `<prefix>/<key>…`. */
function fieldErrors(issues: Issue[], prefix: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const i of issues) {
    const p = i.pointer ?? "";
    if (!p.startsWith(`${prefix}/`)) continue;
    const key = p.slice(prefix.length + 1).split("/")[0];
    if (key && !out[key]) out[key] = i.message;
  }
  return out;
}

export function VisualBuilder({ text, document, onText, schema, shape, sources, issues, onIssue, readOnly }: VisualBuilderProps) {
  // Blocks start closed (each header summarises it), so the whole spec fits on a screen.
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const doc = isObj(document) ? document : null;

  const commit = (next: string | null, done?: [string, string?]) => {
    if (next === null) {
      toast.error("Fix the YAML first", "The spec does not parse, so the builder cannot change it. Switch to the YAML view.");
      return;
    }
    if (next !== text) onText(next);
    if (done) toast.success(done[0], done[1]);
  };
  const toggle = (key: string, value?: boolean) =>
    setOpen((s) => {
      const next = new Set(s);
      if (value ?? !next.has(key)) next.add(key);
      else next.delete(key);
      return next;
    });

  const add = (item: PaletteItem, at = Number.POSITIVE_INFINITY) => {
    if (item.kind === "step") {
      const entry = findStep(sources.catalog, item.id);
      const index = Math.min(at, shape.steps.length);
      const withValue = entry ? starterWith(entry, namesBeforeStep(index, shape, sources)) : {};
      commit(insertStep(text, index, item.id, withValue, pluginId(item.id)), ["Step added", item.id]);
    } else if (item.kind === "model") {
      commit(setModel(text, item.id), ["Model set", item.id]);
      toggle("model", true);
    } else if (item.kind === "dataset") {
      commit(setDataset(text, item.id), ["Dataset set", item.id]);
      toggle("data", true);
    } else {
      commit(addToList(text, ["outputs", "plots"], item.id), ["Plot added", item.id]);
    }
  };

  const blockIssues = (prefix: string) => issues.filter((i) => pointerWithin(i.pointer ?? "", prefix));

  return (
    <div className="grid min-w-0 gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]" data-testid="visual-builder">
      <div className="lg:sticky lg:top-4 lg:self-start">
        <SpecPalette catalog={sources.catalog} datasets={sources.datasets} onAdd={(i) => add(i)} disabled={readOnly || !doc} />
      </div>
      <div className="grid min-w-0 content-start gap-2" role="list" aria-label="Spec blocks">
        {!doc ? (
          <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/8 px-3 py-2 text-[13px]">
            The YAML does not parse, so the blocks cannot be shown. Fix it in the YAML view.
          </p>
        ) : (
          <>
            <Block
              id="metadata"
              title="Metadata"
              summary={typeof asObj(doc.metadata).name === "string" ? (asObj(doc.metadata).name as string) : "unnamed"}
              open={open.has("metadata")}
              onToggle={() => toggle("metadata")}
              issues={blockIssues("/metadata")}
              onIssue={onIssue}
            >
              <MetadataForm doc={doc} schema={schema} errors={fieldErrors(issues, "/metadata")} disabled={readOnly} onChange={(m) => commit(setPath(text, ["metadata"], m))} />
            </Block>

            <Block
              id="data"
              title="Data"
              color={KIND_COLOR.dataset}
              summary={shape.datasetRef ?? "no dataset"}
              open={open.has("data")}
              onToggle={() => toggle("data")}
              accepts={["dataset"]}
              onDropItem={(i) => add(i)}
              issues={blockIssues("/data")}
              onIssue={onIssue}
            >
              <DataForm text={text} doc={doc} schema={schema} shape={shape} sources={sources} commit={commit} disabled={readOnly} />
            </Block>

            <PipelineBlocks text={text} doc={doc} shape={shape} sources={sources} issues={issues} open={open} toggle={toggle} commit={commit} add={add} onIssue={onIssue} disabled={readOnly} />

            <Block
              id="model"
              title="Model"
              color={KIND_COLOR.model}
              summary={shape.modelUse ?? "no model: drop one here"}
              open={open.has("model")}
              onToggle={() => toggle("model")}
              accepts={["model"]}
              onDropItem={(i) => add(i)}
              issues={blockIssues("/model")}
              onIssue={onIssue}
            >
              <ModelForm text={text} doc={doc} schema={schema} shape={shape} sources={sources} issues={issues} commit={commit} disabled={readOnly} />
            </Block>

            <Block
              id="outputs"
              title="Outputs"
              color={KIND_COLOR.plot}
              summary={`${(asObj(doc.outputs).plots as unknown[] | undefined)?.length ?? 0} plots · ${(asObj(doc.outputs).derived as unknown[] | undefined)?.length ?? 0} derived`}
              open={open.has("outputs")}
              onToggle={() => toggle("outputs")}
              accepts={["plot"]}
              onDropItem={(i) => add(i)}
              issues={blockIssues("/outputs")}
              onIssue={onIssue}
            >
              <OutputsForm text={text} doc={doc} shape={shape} sources={sources} commit={commit} disabled={readOnly} />
            </Block>

            <Block
              id="execution"
              title="Execution"
              summary={Object.entries(asObj(doc.execution)).map(([k, v]) => `${k} ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ") || "defaults"}
              open={open.has("execution")}
              onToggle={() => toggle("execution")}
              issues={blockIssues("/execution")}
              onIssue={onIssue}
            >
              <SchemaForm
                schema={defOf(schema, "ExecutionBlock") ?? {}}
                value={asObj(doc.execution)}
                onChange={(next) => commit(setPath(text, ["execution"], Object.keys(next).length ? next : undefined))}
                errors={fieldErrors(issues, "/execution")}
                disabled={readOnly}
              />
            </Block>
          </>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------- block shell

/** A `dragleave` that really leaves the element (not a move onto one of its children). */
function leftFor(e: DragEvent): boolean {
  return !(e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget));
}

function Block({
  id,
  title,
  summary,
  color,
  open,
  onToggle,
  accepts,
  onDropItem,
  issues,
  onIssue,
  actions,
  handle,
  children,
}: {
  id: string;
  title: ReactNode;
  summary?: ReactNode;
  color?: string | undefined;
  open: boolean;
  onToggle: () => void;
  accepts?: PaletteKind[];
  onDropItem?: (item: PaletteItem) => void;
  issues: Issue[];
  onIssue: (issue: Issue) => void;
  actions?: ReactNode;
  handle?: ReactNode;
  children: ReactNode;
}) {
  const [over, setOver] = useState(false);
  const bodyId = useId();
  const errors = issues.filter((i) => (i.severity ?? "error") === "error").length;
  const dropProps = accepts
    ? {
        onDragOver: (e: DragEvent) => {
          if (!carries(e, accepts)) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
          setOver(true);
        },
        onDragLeave: (e: DragEvent) => {
          if (!leftFor(e)) return;
          setOver(false);
        },
        onDrop: (e: DragEvent) => {
          setOver(false);
          const item = readDrop(e);
          if (item && item.kind !== "move" && accepts.includes(item.kind)) {
            e.preventDefault();
            onDropItem?.(item);
          }
        },
      }
    : {};
  return (
    <section
      role="listitem"
      aria-label={typeof title === "string" ? title : undefined}
      data-block={id}
      className={cn("min-w-0 rounded-lg border bg-card transition-shadow", over && "ring-2 ring-primary/60", errors > 0 && "border-destructive/50")}
      style={color ? { borderLeft: `3px solid ${color}` } : undefined}
      {...dropProps}
    >
      <header className="flex min-w-0 items-center gap-1 px-2 py-1.5">
        {handle}
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={bodyId}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-1 py-1 text-left hover:bg-accent/60"
        >
          <ChevronRight className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden />
          <span className="shrink-0 text-[13px] font-semibold">{title}</span>
          {summary ? <span className="min-w-0 truncate font-mono text-xs text-muted-foreground">{summary}</span> : null}
          {issues.length ? (
            <Badge variant={errors ? "danger" : "warning"} className="ml-auto shrink-0">
              {issues.length}
            </Badge>
          ) : null}
        </button>
        {actions}
      </header>
      {open ? (
        <div id={bodyId} className="grid gap-4 border-t px-4 py-3">
          {children}
          {issues.length ? <IssueList issues={issues} onSelect={onIssue} /> : null}
        </div>
      ) : null}
    </section>
  );
}

// ----------------------------------------------------------------------------- metadata

function MetadataForm({ doc, schema, errors, onChange, disabled }: { doc: Obj; schema: unknown; errors: Record<string, string>; onChange: (m: JsonObject | undefined) => void; disabled?: boolean | undefined }) {
  const meta = defOf(schema, "Metadata");
  if (!meta) return null;
  return (
    <SchemaForm
      schema={omitProps(meta, ["about"])}
      value={asObj(doc.metadata)}
      skip={["about"]}
      onChange={(next) => onChange({ ...next, ...(asObj(doc.metadata).about !== undefined ? { about: asObj(doc.metadata).about! } : {}) })}
      errors={errors}
      disabled={disabled}
    />
  );
}

// ----------------------------------------------------------------------------- data

interface FormProps {
  text: string;
  doc: Obj;
  shape: SpecShape;
  sources: HintSources;
  commit: (next: string | null, done?: [string, string?]) => void;
  disabled?: boolean | undefined;
}

function DataForm({ text, doc, schema, shape, sources, commit, disabled }: FormProps & { schema: unknown }) {
  const id = useId();
  const data = asObj(doc.data);
  const current = typeof data.dataset === "string" ? data.dataset : "";
  const known = sources.datasets.map((d) => `${d.ref}@latest`);
  const columns = namesOf(sources.columns);
  const model = shape.modelUse ? findModel(sources.catalog, shape.modelUse) : null;
  const wanted = templateNames(model);
  const split = asObj(data.split);
  const kinds = splitKinds(schema);
  const kind = typeof split.kind === "string" ? split.kind : "";
  const splitDef = kinds.find((k) => k.kind === kind);
  const splitSchema = splitDef ? defOf(schema, splitDef.def) : null;
  const rawDims = namesBeforeStep(0, shape, sources).dims.map((d) => d.name);

  return (
    <>
      <Field label="Dataset" htmlFor={`${id}-ds`} hint="Or drop a dataset from the palette on this block.">
        <NativeSelect id={`${id}-ds`} value={current} disabled={disabled} onChange={(e) => commit(setDataset(text, e.target.value))}>
          <option value="" disabled>
            Choose a dataset
          </option>
          {current && !known.includes(current) ? <option value={current}>{current}</option> : null}
          {sources.datasets.map((d) => (
            <option key={d.ref} value={`${d.ref}@latest`}>
              {d.name} (latest)
            </option>
          ))}
        </NativeSelect>
      </Field>

      {(["dims", "vars"] as const).map((kindOf) => {
        const bound = shape.bind[kindOf];
        const canon = [...new Set([...(kindOf === "dims" ? wanted.dims : wanted.vars), ...Object.keys(bound)])];
        const options = (kindOf === "dims" ? columns.dims : columns.vars).map((c) => c.name);
        return (
          <BindingTable
            key={kindOf}
            label={kindOf === "dims" ? "Bind dims" : "Bind variables"}
            hint={model ? `Canonical names ${model.id} expects, and the dataset column each one reads.` : "Canonical name → dataset column. Pick a model to see the names it expects."}
            canonical={canon}
            wanted={new Set(kindOf === "dims" ? wanted.dims : wanted.vars)}
            bound={bound}
            options={options}
            disabled={disabled}
            onSet={(c, col) => commit(setPath(text, ["data", "bind", kindOf, c], col || undefined))}
          />
        );
      })}

      <fieldset className="grid gap-3 rounded-md border p-3">
        <legend className="px-1 text-[13px] font-medium">Train / holdout split</legend>
        <Field label="Kind" htmlFor={`${id}-split`}>
          <NativeSelect
            id={`${id}-split`}
            value={kind}
            disabled={disabled}
            onChange={(e) => commit(setPath(text, ["data", "split"], e.target.value ? { kind: e.target.value } : undefined))}
          >
            <option value="">No split (fit on everything)</option>
            {kinds.map((k) => (
              <option key={k.kind} value={k.kind}>
                {k.kind.replace(/_/g, " ")}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {splitSchema ? (
          <SchemaForm
            schema={omitProps(splitSchema, ["kind"])}
            value={omitKind(split)}
            onChange={(next) => commit(setPath(text, ["data", "split"], { kind, ...next }))}
            suggest={{ dim: rawDims, dims: rawDims, within: rawDims, after_step: shape.steps.map((s) => s.id) }}
            disabled={disabled}
            nested
          />
        ) : null}
      </fieldset>
    </>
  );
}

function omitKind(o: JsonObject): JsonObject {
  const { kind: _, ...rest } = o;
  return rest;
}

function BindingTable({
  label,
  hint,
  canonical,
  wanted,
  bound,
  options,
  onSet,
  disabled,
}: {
  label: string;
  hint: string;
  canonical: string[];
  wanted: Set<string>;
  bound: Record<string, string>;
  options: string[];
  onSet: (canonical: string, column: string) => void;
  disabled?: boolean | undefined;
}) {
  const id = useId();
  const [extra, setExtra] = useState("");
  return (
    <fieldset className="grid gap-2 rounded-md border p-3">
      <legend className="px-1 text-[13px] font-medium">{label}</legend>
      <p className="-mt-1 text-xs text-muted-foreground">{hint}</p>
      {canonical.length ? (
        <div className="grid grid-cols-[minmax(6rem,auto)_minmax(0,1fr)] items-center gap-x-3 gap-y-1.5">
          {canonical.map((c) => (
            <BindingRow key={c} id={`${id}-${c}`} canonical={c} expected={wanted.has(c)} column={bound[c] ?? ""} options={options} onSet={onSet} disabled={disabled} />
          ))}
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        <Input
          aria-label={`${label}: new canonical name`}
          placeholder="Another canonical name"
          className="h-8 font-mono text-xs"
          value={extra}
          disabled={disabled}
          onChange={(e) => setExtra(e.target.value.trim())}
        />
        <Button
          size="sm"
          variant="outline"
          disabled={disabled || !extra || canonical.includes(extra) || !options.length}
          onClick={() => {
            onSet(extra, options[0]!);
            setExtra("");
          }}
        >
          Bind
        </Button>
      </div>
    </fieldset>
  );
}

function BindingRow({
  id,
  canonical,
  expected,
  column,
  options,
  onSet,
  disabled,
}: {
  id: string;
  canonical: string;
  expected: boolean;
  column: string;
  options: string[];
  onSet: (canonical: string, column: string) => void;
  disabled?: boolean | undefined;
}) {
  return (
    <>
      <label htmlFor={id} className="font-mono text-xs">
        {canonical}
        {expected ? null : <span className="ml-1 text-muted-foreground">(custom)</span>}
      </label>
      <NativeSelect id={id} className="h-8" value={column} disabled={disabled} onChange={(e) => onSet(canonical, e.target.value)}>
        <option value="">{expected ? "Not bound (keeps its dataset name)" : "Remove"}</option>
        {column && !options.includes(column) ? <option value={column}>{column}</option> : null}
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </NativeSelect>
    </>
  );
}

// ----------------------------------------------------------------------------- pipeline

function DropZone({ index, onDrop, disabled, label }: { index: number; onDrop: (index: number, e: DragEvent) => void; disabled?: boolean | undefined; label: string }) {
  const [over, setOver] = useState(false);
  return (
    <div
      role="presentation"
      aria-label={label}
      data-drop-index={index}
      onDragOver={(e) => {
        if (disabled || !carries(e, ["step", "move"])) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = carries(e, ["move"]) ? "move" : "copy";
        setOver(true);
      }}
      onDragLeave={(e) => {
        if (leftFor(e)) setOver(false);
      }}
      onDrop={(e) => {
        setOver(false);
        onDrop(index, e);
      }}
      // A fixed height: a zone that grew under the pointer would move the drop target away.
      className="relative -my-1.5 h-5"
    >
      <div aria-hidden className={cn("pointer-events-none absolute inset-x-6 top-1/2 h-1 -translate-y-1/2 rounded-full transition-colors", over ? "bg-primary" : "bg-transparent")} />
    </div>
  );
}

function PipelineBlocks({
  text,
  doc,
  shape,
  sources,
  issues,
  open,
  toggle,
  commit,
  add,
  onIssue,
  disabled,
}: FormProps & {
  issues: Issue[];
  open: Set<string>;
  toggle: (key: string, value?: boolean) => void;
  add: (item: PaletteItem, at?: number) => void;
  onIssue: (issue: Issue) => void;
}) {
  const pipeline = Array.isArray(doc.pipeline) ? doc.pipeline : [];
  const drop = (index: number, e: DragEvent) => {
    const item = readDrop(e);
    if (!item) return;
    e.preventDefault();
    if (item.kind === "move") {
      const to = index > item.from ? index - 1 : index;
      if (to !== item.from) commit(moveStep(text, item.from, to));
    } else if (item.kind === "step") {
      add(item, index);
    }
  };
  return (
    <div className="grid gap-2" role="group" aria-label="Pipeline">
      <div className="flex items-center gap-2 px-1 pt-1 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Pipeline <span className="font-normal normal-case">{pipeline.length ? `${pipeline.length} step${pipeline.length === 1 ? "" : "s"}, in order` : "no steps: drag one here from the palette"}</span>
      </div>
      <DropZone index={0} onDrop={drop} disabled={disabled} label="Drop a step at the start" />
      {pipeline.map((raw, i) => {
        const step = asObj(raw);
        const sid = typeof step.id === "string" ? step.id : `step ${i + 1}`;
        return (
          <div key={`${sid}-${i}`} className="grid gap-2">
            <StepBlock
              index={i}
              count={pipeline.length}
              step={step}
              text={text}
              doc={doc}
              shape={shape}
              sources={sources}
              issues={issues.filter((x) => pointerWithin(x.pointer ?? "", `/pipeline/${i}`))}
              errors={fieldErrors(issues, `/pipeline/${i}/with`)}
              open={open.has(`step:${sid}`)}
              onToggle={() => toggle(`step:${sid}`)}
              commit={commit}
              onIssue={onIssue}
              disabled={disabled}
            />
            <DropZone index={i + 1} onDrop={drop} disabled={disabled} label={`Drop a step after ${sid}`} />
          </div>
        );
      })}
    </div>
  );
}

function StepBlock({
  index,
  count,
  step,
  text,
  shape,
  sources,
  issues,
  errors,
  open,
  onToggle,
  commit,
  onIssue,
  disabled,
}: FormProps & {
  index: number;
  count: number;
  step: JsonObject;
  issues: Issue[];
  errors: Record<string, string>;
  open: boolean;
  onToggle: () => void;
  onIssue: (issue: Issue) => void;
}) {
  const sid = typeof step.id === "string" ? step.id : "";
  const use = typeof step.use === "string" ? step.use : "";
  const entry = use ? findStep(sources.catalog, use) : null;
  const names = namesBeforeStep(index, shape, sources);
  const settings = entry?.settings_schema ? inlineRefs(entry.settings_schema) : null;
  const suggest: Record<string, string[]> = {};
  for (const p of [...(entry?.var_params ?? []), ...(entry?.target_vars_param ? [entry.target_vars_param] : [])]) suggest[p] = names.vars.map((n) => n.name);
  for (const p of entry?.dim_params ?? []) suggest[p] = names.dims.map((n) => n.name);

  return (
    <Block
      id={`step:${sid}`}
      title={sid || `step ${index + 1}`}
      color={KIND_COLOR.step}
      summary={entry?.about?.title ? `${use} · ${entry.about.title}` : use}
      open={open}
      onToggle={onToggle}
      issues={issues}
      onIssue={onIssue}
      handle={
        <span
          draggable={!disabled}
          onDragStart={(e) => {
            e.dataTransfer.setData(MOVE_MIME, String(index));
            e.dataTransfer.effectAllowed = "move";
          }}
          title="Drag to reorder"
          aria-hidden
          className={cn("rounded p-1 text-muted-foreground", !disabled && "cursor-grab hover:bg-accent active:cursor-grabbing")}
          data-drag-step={index}
        >
          <GripVertical className="size-4" />
        </span>
      }
      actions={
        <span className="flex shrink-0 items-center">
          <Button size="icon" variant="ghost" className="size-7" aria-label={`Move ${sid} up`} disabled={disabled || index === 0} onClick={() => commit(moveStep(text, index, index - 1))}>
            <ArrowUp />
          </Button>
          <Button size="icon" variant="ghost" className="size-7" aria-label={`Move ${sid} down`} disabled={disabled || index === count - 1} onClick={() => commit(moveStep(text, index, index + 1))}>
            <ArrowDown />
          </Button>
          <Button size="icon" variant="ghost" className="size-7" aria-label={`Remove ${sid}`} disabled={disabled} onClick={() => commit(removeStep(text, index), ["Step removed", sid])}>
            <Trash2 />
          </Button>
        </span>
      }
    >
      <StepIdField index={index} current={sid} taken={shape.steps.map((s) => s.id)} text={text} commit={commit} disabled={disabled} />
      {entry?.summary_plain ?? entry?.summary ? <p className="text-xs text-muted-foreground">{entry.summary_plain ?? entry.summary}</p> : null}
      {settings ? (
        <SchemaForm
          schema={settings}
          value={asObj(step.with)}
          onChange={(next) => commit(setPath(text, ["pipeline", index, "with"], Object.keys(next).length ? next : undefined))}
          errors={errors}
          suggest={suggest}
          disabled={disabled}
          nested
          empty="This step has no settings."
        />
      ) : (
        <p className="text-[13px] text-muted-foreground">{use ? `${use} is not in the catalog; edit its settings in the YAML view.` : "This step has no `use`."}</p>
      )}
    </Block>
  );
}

function StepIdField({ index, current, taken, text, commit, disabled }: { index: number; current: string; taken: string[]; text: string; commit: FormProps["commit"]; disabled?: boolean | undefined }) {
  const id = useId();
  const [draft, setDraft] = useState(current);
  useEffect(() => setDraft(current), [current]);
  const problem =
    draft === current ? null : !STEP_ID.test(draft) ? "Lower case letters, digits and _, starting with a letter or _." : taken.includes(draft) ? "Another step has this id." : null;
  return (
    <Field label="Step id" htmlFor={id} error={problem} hint="Later steps and the split's after_step refer to it; renaming updates the split.">
      <Input
        id={id}
        className="h-8 font-mono"
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          if (draft !== current && !problem) commit(renameStep(text, index, draft));
          else setDraft(current);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
    </Field>
  );
}

// ----------------------------------------------------------------------------- model

function ModelForm({ text, doc, schema, shape, sources, issues, commit, disabled }: FormProps & { schema: unknown; issues: Issue[] }) {
  const id = useId();
  const block = asObj(doc.model);
  const model = shape.modelUse ? findModel(sources.catalog, shape.modelUse) : null;
  const models = latestModels(sources.catalog);
  const current = shape.modelUse ?? "";
  const currentId = current ? pluginId(current) : "";
  const fit = defOf(schema, "FitConfig");
  const names = namesForModel(shape, sources);
  const settingsSchema = model?.settings_schema ? inlineRefs(model.settings_schema) : null;
  // Model settings that name dims or variables (by the conventions the plugins share).
  const suggest = Object.fromEntries(
    ["controls", "covariates", "group_covariates", "target", "pool_over", "group_dim", "time_dim"].map((k) => [k, k.endsWith("dim") || k === "pool_over" ? names.dims.map((n) => n.name) : names.vars.map((n) => n.name)]),
  );
  return (
    <>
      <Field label="Model" htmlFor={`${id}-use`} hint={model?.about?.summary_plain ?? "Or drop a model from the palette on this block. A different model clears the settings."}>
        <NativeSelect
          id={`${id}-use`}
          value={models.some((m) => m.id === currentId) ? currentId : current}
          disabled={disabled}
          onChange={(e) => {
            const m = models.find((x) => x.id === e.target.value);
            commit(setModel(text, m ? pinnedUse(m.id, m.version) : e.target.value));
          }}
        >
          <option value="" disabled>
            Choose a model
          </option>
          {current && !models.some((m) => m.id === currentId) ? <option value={current}>{current}</option> : null}
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.about?.title ? `${m.about.title} (${m.id})` : m.id}
            </option>
          ))}
        </NativeSelect>
      </Field>
      {settingsSchema ? (
        <fieldset className="grid gap-3 rounded-md border p-3">
          <legend className="px-1 text-[13px] font-medium">Settings</legend>
          <SchemaForm
            schema={settingsSchema}
            value={asObj(block.settings)}
            onChange={(next) => commit(setPath(text, ["model", "settings"], Object.keys(next).length ? next : undefined))}
            errors={fieldErrors(issues, "/model/settings")}
            suggest={suggest}
            disabled={disabled}
            nested
            empty="This model has no settings."
          />
        </fieldset>
      ) : current ? (
        <p className="text-[13px] text-muted-foreground">{model ? "This model has no settings." : `${current} is not in the catalog; edit its settings in the YAML view.`}</p>
      ) : null}
      {fit && current ? (
        <details className="rounded-md border p-3">
          <summary className="cursor-pointer text-[13px] font-medium">Fit</summary>
          <div className="mt-3">
            <SchemaForm
              schema={fit}
              value={asObj(block.fit)}
              onChange={(next) => commit(setPath(text, ["model", "fit"], Object.keys(next).length ? next : undefined))}
              errors={fieldErrors(issues, "/model/fit")}
              disabled={disabled}
              nested
            />
          </div>
        </details>
      ) : null}
    </>
  );
}

// ----------------------------------------------------------------------------- outputs

function OutputsForm({ text, doc, shape, sources, commit, disabled }: FormProps) {
  const outputs = asObj(doc.outputs);
  const plots = Array.isArray(outputs.plots) ? outputs.plots.filter((p): p is string => typeof p === "string") : [];
  const derived = Array.isArray(outputs.derived) ? outputs.derived.filter((p): p is string => typeof p === "string") : [];
  const model = shape.modelUse ? findModel(sources.catalog, shape.modelUse) : null;
  const plotOptions = plotSnippets(sources.catalog, model);
  const toggleIn = (key: "plots" | "derived", list: string[], value: string) => {
    const next = list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
    commit(setPath(text, ["outputs", key], next.length ? next : undefined));
  };
  return (
    <>
      <ChipGroup
        label="Plots"
        hint="Click to include or leave out. Recommended by the model first; drop more from the palette."
        options={[...plotOptions.map((p) => ({ value: p.body, title: p.description ?? p.body })), ...plots.filter((p) => !plotOptions.some((o) => o.body === p)).map((p) => ({ value: p, title: p }))]}
        selected={plots}
        onToggle={(v) => toggleIn("plots", plots, v)}
        disabled={disabled}
      />
      {model?.derived?.length || derived.length ? (
        <ChipGroup
          label="Derived outputs"
          hint={model ? `Quantities ${model.id} computes after the fit.` : "Quantities the model computes after the fit."}
          options={[...(model?.derived ?? []).map((d) => ({ value: d.name, title: d.description ?? d.name })), ...derived.filter((d) => !model?.derived?.some((x) => x.name === d)).map((d) => ({ value: d, title: d }))]}
          selected={derived}
          onToggle={(v) => toggleIn("derived", derived, v)}
          disabled={disabled}
        />
      ) : null}
    </>
  );
}

function ChipGroup({
  label,
  hint,
  options,
  selected,
  onToggle,
  disabled,
}: {
  label: string;
  hint: string;
  options: { value: string; title: string }[];
  selected: string[];
  onToggle: (value: string) => void;
  disabled?: boolean | undefined;
}) {
  return (
    <fieldset className="grid gap-2">
      <legend className="text-[13px] font-medium">{label}</legend>
      <p className="text-xs text-muted-foreground">{hint}</p>
      <div className="flex flex-wrap gap-1.5">
        {options.map((o) => {
          const on = selected.includes(o.value);
          return (
            <button
              key={o.value}
              type="button"
              aria-pressed={on}
              title={o.title}
              disabled={disabled}
              onClick={() => onToggle(o.value)}
              className={cn("rounded-md border px-2 py-0.5 font-mono text-xs transition-colors hover:bg-accent", on ? "border-primary/50 bg-primary/10 text-foreground" : "text-muted-foreground")}
            >
              {o.value}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}
