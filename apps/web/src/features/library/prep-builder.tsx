/**
 * Make your own › Prep: the prep preset builder (v0.18 D4, §3.4).
 *
 * A prep preset is the steps you always apply, saved once and reused: pick steps from the same
 * catalog as the spec builder (a select, so the keyboard works everywhere), set each one up with
 * its own settings form, and mark the columns that change from dataset to dataset as **slots**
 * ("ask for this when used"). "Try it on a dataset" fills the slots and checks the steps with
 * `POST /v1/specs:validate`, so you see what each stage of the data looks like before saving.
 * A `core.formula` step is "your own step with no code"; the server checks only its size here
 * and parses it when the steps run (R8).
 */
import { ArrowDown, ArrowUp, Plus, Save, Trash2 } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";

import { can } from "@/api/access";
import { validateSpec } from "@/api/queries";
import type { Issue, StepCatalogEntry } from "@/api/types";
import { ErrorNotice, IssueList, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { SchemaForm, type JsonObject as FormObject } from "@/features/forms/json-schema-form";
import { safeSchema } from "@/features/forms/schema-sanitize";
import { useQuery } from "@tanstack/react-query";
import { cn } from "@/lib/utils";

import { useSaveDoc, type Saved } from "./actions";
import { useStepCatalog } from "./catalog";
import { docKey, isStaleRevision, useLibraryItem, useLibraryValidation } from "./api";
import { DatasetPicker, useDatasetColumns, type ColumnInfo } from "./data-source";
import { API_VERSION, fillPrepSlots, identifier, moveItem, slotOfValue, slotsUsed, slotValue, specYaml, tidyDoc } from "./doc";
import { BuilderSection, Chip, focusPointer, IssueLines, issueText, LIME, MetadataFields, PILL, ScopeChoice, ToggleChip, useDebounced } from "./ui";
import { LazyYamlTab as YamlTab } from "./yaml-tab-lazy";
import { isPrepPresetDoc, MAX_SLOTS, slotName, splitUseRef, type Json, type JsonObject, type LibraryDoc, type LibraryScope, type PrepPresetDoc, type Slot, type StepCallDoc } from "./types";

const VALIDATE_DEBOUNCE_MS = 300;

function stepTitle(entry: StepCatalogEntry | undefined, use: string): string {
  return entry?.about?.title ?? splitUseRef(use)[0];
}

function major(version: string): string {
  return version.split(".")[0] ?? "1";
}

/** The `with` keys of a step that can be slots: its variable and dim parameters. */
function slotKeys(entry: StepCatalogEntry | undefined): { key: string; kind: Slot["kind"] }[] {
  if (!entry) return [];
  const props = ((safeSchema((entry.settings_schema ?? {}) as FormObject).properties as FormObject | undefined) ?? {}) as Record<string, FormObject>;
  const out: { key: string; kind: Slot["kind"] }[] = [];
  for (const key of entry.var_params ?? []) out.push({ key, kind: props[key]?.type === "array" ? "var_list" : "var" });
  for (const key of entry.dim_params ?? []) if (!out.some((o) => o.key === key)) out.push({ key, kind: "dim" });
  return out;
}

function withErrors(issues: readonly Issue[] | undefined, index: number): Record<string, string> {
  const prefix = `/steps/${index}/with/`;
  const out: Record<string, string> = {};
  for (const i of issues ?? []) {
    const p = i.pointer ?? "";
    if (p.startsWith(prefix) && (i.severity ?? "error") !== "info") out[p.slice(prefix.length).split("/")[0] ?? ""] ??= i.message;
  }
  return out;
}

// ------------------------------------------------------------------------------ one step

function StepCard({
  step,
  index,
  count,
  entry,
  slots,
  issues,
  onChange,
  onSlots,
  onMove,
  onRemove,
}: {
  step: StepCallDoc;
  index: number;
  count: number;
  entry: StepCatalogEntry | undefined;
  slots: Record<string, Slot>;
  issues: readonly Issue[] | undefined;
  onChange: (s: StepCallDoc) => void;
  onSlots: (slots: Record<string, Slot>, step: StepCallDoc) => void;
  onMove: (to: number) => void;
  onRemove: () => void;
}) {
  const id = useId();
  const ptr = `/steps/${index}`;
  const title = stepTitle(entry, step.use);
  const w = (step.with ?? {}) as Record<string, Json>;
  const slotted = Object.entries(w).filter(([, v]) => slotOfValue(v) !== null).map(([k]) => k);
  const schema = useMemo(() => safeSchema((entry?.settings_schema ?? {}) as FormObject), [entry]);

  const toggleSlot = (key: string, kind: Slot["kind"]) => {
    const current = slotOfValue(w[key]);
    if (current !== null) {
      const nextWith = { ...w };
      delete nextWith[key];
      const next = { ...step, with: nextWith };
      const still = slotsUsed([next]);
      const nextSlots = { ...slots };
      if (!still.has(current)) delete nextSlots[current];
      onSlots(nextSlots, next);
      return;
    }
    let name = identifier(key);
    for (let n = 2; name in slots; n++) name = identifier(`${key}_${n}`);
    const label = kind === "dim" ? `${title}: which dimension (${key})` : kind === "var_list" ? `${title}: which columns (${key})` : `${title}: which column (${key})`;
    onSlots({ ...slots, [name]: { kind, label: label.slice(0, 80) } }, { ...step, with: { ...w, [key]: slotValue(name, kind === "var_list") } });
  };

  return (
    <li className="rounded-2xl border bg-background p-4 sm:p-5" data-step={step.id || `#${index}`} aria-label={`Step ${index + 1}: ${title}`}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-lg leading-tight font-bold">
            <span className="mr-2 text-muted-foreground tabular">{index + 1}.</span>
            {title}
          </h3>
          <p className="font-mono text-xs text-muted-foreground">{step.use}</p>
          {entry?.about?.summary_plain ?? entry?.summary_plain ? <p className="mt-1 max-w-[65ch] text-sm text-muted-foreground">{entry?.about?.summary_plain ?? entry?.summary_plain}</p> : null}
        </div>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon-sm" data-action="up" aria-label={`Move ${title} up`} disabled={index === 0} onClick={() => onMove(index - 1)}>
            <ArrowUp />
          </Button>
          <Button variant="ghost" size="icon-sm" data-action="down" aria-label={`Move ${title} down`} disabled={index === count - 1} onClick={() => onMove(index + 1)}>
            <ArrowDown />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label={`Remove ${title}`} onClick={onRemove}>
            <Trash2 />
          </Button>
        </div>
      </div>
      {!entry ? <p className="mb-3 text-sm text-warning">This step is not in the catalog of this server. It cannot run until someone installs it.</p> : null}
      <div className="grid gap-4">
        <Field label="Step id" htmlFor={`${id}-id`} hint="Unique in the preset. Letters, digits and _." error={issueText(issues, `${ptr}/id`)} className="max-w-xs">
          <Input id={`${id}-id`} data-pointer={`${ptr}/id`} className="font-mono text-xs" value={step.id} onChange={(e) => onChange({ ...step, id: e.target.value })} />
        </Field>
        {slotKeys(entry).length ? (
          <div className="grid gap-2">
            <span className="text-sm font-medium">Ask for these when the preset is used</span>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={`Slots of ${title}`}>
              {slotKeys(entry).map(({ key, kind }) => (
                <ToggleChip key={key} pressed={slotOfValue(w[key]) !== null} onClick={() => toggleSlot(key, kind)} disabled={slotOfValue(w[key]) === null && Object.keys(slots).length >= MAX_SLOTS}>
                  <span className="font-mono">{key}</span>
                </ToggleChip>
              ))}
            </div>
          </div>
        ) : null}
        {slotted.length ? (
          <ul className="flex flex-wrap gap-1.5">
            {slotted.map((k) => (
              <li key={k}>
                <Chip tone="lime">
                  <span className="font-mono">{k}</span>: asked when used ({slots[slotOfValue(w[k]) ?? ""]?.label ?? slotOfValue(w[k])})
                </Chip>
              </li>
            ))}
          </ul>
        ) : null}
        <div data-pointer={`${ptr}/with`}>
          <SchemaForm schema={schema} value={w} skip={slotted} errors={withErrors(issues, index)} onChange={(next) => onChange({ ...step, with: { ...next, ...Object.fromEntries(slotted.map((k) => [k, w[k] ?? null])) } })} nested empty="This step has no settings." />
        </div>
        <IssueLines issues={issues} pointer={ptr} />
      </div>
    </li>
  );
}

// ------------------------------------------------------------------------------ try it

function SlotInput({ slot, name, value, onChange, columns }: { slot: Slot; name: string; value: Json | undefined; onChange: (v: Json | undefined) => void; columns: ColumnInfo[] }) {
  const id = useId();
  const options = columns.filter((c) => (slot.kind === "dim" ? c.kind === "dim" : c.kind === "var"));
  if (slot.kind === "var_list") {
    const picked = Array.isArray(value) ? value.map(String) : [];
    return (
      <fieldset className="grid gap-1.5">
        <legend className="mb-1 text-sm font-medium">{slot.label}</legend>
        <div className="flex flex-wrap gap-1.5">
          {options.map((c) => (
            <ToggleChip key={c.name} pressed={picked.includes(c.name)} onClick={() => onChange(picked.includes(c.name) ? picked.filter((x) => x !== c.name) : [...picked, c.name])}>
              {c.name}
            </ToggleChip>
          ))}
        </div>
      </fieldset>
    );
  }
  return (
    <Field label={slot.label} htmlFor={`${id}-${name}`}>
      <NativeSelect id={`${id}-${name}`} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value || undefined)}>
        <option value="">Pick one</option>
        {options.map((c) => (
          <option key={c.name} value={c.name}>
            {c.name}
          </option>
        ))}
      </NativeSelect>
    </Field>
  );
}

function StagePreview({ doc }: { doc: PrepPresetDoc }) {
  const [dataset, setDataset] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, Json>>({});
  const data = useDatasetColumns(dataset);
  const filled = useMemo(() => fillPrepSlots(doc, values), [doc, values]);
  const spec = useMemo<JsonObject | null>(
    () =>
      dataset && !filled.missing.length && filled.steps.length
        ? { apiVersion: API_VERSION, kind: "ModelSpec", metadata: { name: "prep-preview", project: "default" }, data: { dataset }, pipeline: filled.steps as unknown as Json[], model: { use: "tabular.declarative_glm@^1" } }
        : null,
    [dataset, filled],
  );
  const debounced = useDebounced(spec, 400);
  const check = useQuery({
    queryKey: ["library", "prep-preview", debounced],
    queryFn: async ({ signal }) => validateSpec(debounced ? await specYaml(debounced) : "", signal),
    enabled: Boolean(debounced),
    retry: false,
    staleTime: 60_000,
  });
  const pipelineIssues = (check.data?.issues ?? []).filter((i) => (i.pointer ?? "").startsWith("/pipeline") || (i.pointer ?? "").startsWith("/data"));
  const slotEntries = Object.entries(doc.slots ?? {});
  return (
    <div className="grid gap-4">
      <div className="max-w-lg">
        <DatasetPicker value={dataset} onChange={setDataset} label="Try it on a dataset" hint="Nothing runs: the server checks each step and shows the data after it." />
      </div>
      {dataset && slotEntries.length ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {slotEntries.map(([name, slot]) => (
            <SlotInput key={name} name={name} slot={slot} value={values[name]} columns={data.columns} onChange={(v) => setValues((cur) => { const next = { ...cur }; if (v === undefined) delete next[name]; else next[name] = v; return next; })} />
          ))}
        </div>
      ) : null}
      {dataset && filled.missing.length ? <p className="text-sm text-muted-foreground">Fill {filled.missing.map((m) => doc.slots?.[m]?.label ?? m).join(", ")} to see the stages.</p> : null}
      {check.isFetching ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Spinner /> Checking the steps…
        </p>
      ) : null}
      {check.isError ? <ErrorNotice error={check.error} title="The check did not run" /> : null}
      {check.data ? (
        <div className="grid gap-3">
          {pipelineIssues.length ? <IssueList issues={pipelineIssues} /> : <p className="text-sm text-success">The steps fit this dataset.</p>}
          <ol className="grid gap-2" aria-label="The data after each stage">
            {(check.data.stages ?? []).map((st) => (
              <li key={st.name} className="rounded-xl border bg-background p-3">
                <p className="text-sm font-semibold">
                  {st.name === "raw" ? "Your data as uploaded" : `After ${st.name}`}
                  {st.split ? <Chip className="ml-2">train/holdout split here</Chip> : null}
                </p>
                <p className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                  {st.data_schema
                    ? `${Object.keys(st.data_schema.vars ?? {}).join(", ") || "no variables"} · dims ${Object.keys(st.data_schema.dims ?? {}).join(", ") || "none"}`
                    : "The shape is only known when the step runs."}
                </p>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------ the builder

export interface PrepBuilderProps {
  itemId: string | null;
  initial: PrepPresetDoc;
  onCreated?: (saved: Saved) => void;
}

export function PrepBuilder({ itemId, initial, onCreated }: PrepBuilderProps) {
  const id = useId();
  const grants = useGrants();
  const item = useLibraryItem(itemId ?? undefined);
  const catalog = useStepCatalog();
  const [doc, setDoc] = useState<PrepPresetDoc>(initial);
  const [adding, setAdding] = useState("");
  const [scope, setScope] = useState<LibraryScope>("project");
  const [view, setView] = useState<"builder" | "yaml">("builder");
  const [savedKey, setSavedKey] = useState<string | null>(itemId ? docKey(tidyDoc(initial)) : null);
  const root = useRef<HTMLDivElement>(null);
  const save = useSaveDoc(item.data);

  const tidy = useMemo(() => tidyDoc(doc), [doc]);
  const debounced = useDebounced(tidy, VALIDATE_DEBOUNCE_MS);
  const validation = useLibraryValidation(debounced as unknown as JsonObject);
  const issues = validation.data?.issues;
  const checking = validation.isFetching || docKey(debounced) !== docKey(tidy);
  const dirty = docKey(tidy) !== savedKey;
  const errors = (issues ?? []).filter((i) => (i.severity ?? "error") === "error").length;
  const steps = doc.steps;
  const slots = doc.slots ?? {};

  const setSteps = (next: StepCallDoc[]) => setDoc({ ...doc, steps: next });
  const addStep = () => {
    const entry = catalog.byId.get(adding);
    if (!entry) return;
    let stepId = identifier(entry.id.split(".").at(-1) ?? "step");
    for (let n = 2; steps.some((s) => s.id === stepId); n++) stepId = identifier(`${entry.id.split(".").at(-1) ?? "step"}_${n}`);
    setSteps([...steps, { id: stepId, use: `${entry.id}@^${major(entry.version)}`, with: {} }]);
    setAdding("");
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-step="${stepId}"] input`)?.focus());
  };
  const doSave = async () => {
    try {
      const saved = await save.save(doc, { scope });
      setSavedKey(docKey(tidy));
      if (!itemId) onCreated?.(saved);
    } catch (e) {
      if (isStaleRevision(e)) void item.refetch();
    }
  };

  const groups = useMemo(() => {
    const out = new Map<string, StepCatalogEntry[]>();
    for (const s of catalog.steps) out.set(s.kind, [...(out.get(s.kind) ?? []), s]);
    return [...out.entries()];
  }, [catalog.steps]);

  return (
    <div ref={root} className="grid gap-6">
      <SegmentedControl label="How to edit" value={view} onValueChange={setView} options={[{ value: "builder", label: "Builder" }, { value: "yaml", label: "YAML" }]} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)]">
        <div className="grid min-w-0 content-start gap-5">
          {view === "yaml" ? (
            <YamlTab kind="prep_preset" doc={doc} issues={issues} onDoc={(d: LibraryDoc) => isPrepPresetDoc(d) && setDoc(d)} />
          ) : (
            <>
              <BuilderSection n={1} title="Add the steps you always do" hint="They run in this order. Mark the columns that change from dataset to dataset as slots: people pick them when they use the preset." done={steps.length > 0}>
                {steps.length ? (
                  <ol className="grid gap-3" aria-label="Steps">
                    {steps.map((s, i) => (
                      <StepCard
                        key={`${s.id}-${i}`}
                        step={s}
                        index={i}
                        count={steps.length}
                        entry={catalog.byId.get(splitUseRef(s.use)[0])}
                        slots={slots}
                        issues={issues}
                        onChange={(next) => setSteps(steps.map((x, j) => (j === i ? next : x)))}
                        onSlots={(nextSlots, next) => setDoc({ ...doc, slots: nextSlots, steps: steps.map((x, j) => (j === i ? next : x)) })}
                        onMove={(to) => {
                          setSteps(moveItem(steps, i, to));
                          requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-step="${s.id}"] [data-action="${to < i ? "up" : "down"}"]:not(:disabled), [data-step="${s.id}"] [data-action]`)?.focus());
                        }}
                        onRemove={() => {
                          const next = steps.filter((_, j) => j !== i);
                          const used = slotsUsed(next);
                          setDoc({ ...doc, steps: next, slots: Object.fromEntries(Object.entries(slots).filter(([k]) => used.has(k))) });
                        }}
                      />
                    ))}
                  </ol>
                ) : (
                  <p className="rounded-xl border border-dashed border-border-strong p-4 text-muted-foreground">No steps yet. Start with the one you always do first: a weekly total, filling gaps, a log…</p>
                )}
                <IssueLines issues={issues} pointer="/steps" className="mt-2" />
                <div className="mt-4 flex flex-wrap items-end gap-2" data-pointer="/steps">
                  <Field label="Add a step" htmlFor={`${id}-add`} className="min-w-56 flex-1 sm:max-w-sm">
                    <NativeSelect id={`${id}-add`} value={adding} disabled={catalog.isPending} onChange={(e) => setAdding(e.target.value)}>
                      <option value="">{catalog.isPending ? "Loading the catalog…" : "Pick a step"}</option>
                      {groups.map(([kind, list]) => (
                        <optgroup key={kind} label={`${kind} steps`}>
                          {list.map((s) => (
                            <option key={s.id} value={s.id}>
                              {s.about?.title ? `${s.about.title} (${s.id})` : s.id}
                              {s.provider ? " · from your team" : ""}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </NativeSelect>
                  </Field>
                  <Button variant="secondary" className={PILL} disabled={!adding} onClick={addStep}>
                    <Plus /> Add the step
                  </Button>
                </div>
              </BuilderSection>

              {Object.keys(slots).length ? (
                <BuilderSection n={2} title="Name the slots" hint="This is the question people see when they use the preset.">
                  <ul className="grid gap-3">
                    {Object.entries(slots).map(([name, slot]) => (
                      <li key={name} className="grid items-end gap-3 rounded-xl border bg-background p-3 sm:grid-cols-[minmax(0,1fr)_auto]" data-pointer={`/slots/${name}`}>
                        <Field label={`Question for ${name}`} htmlFor={`${id}-slot-${name}`} error={issueText(issues, `/slots/${name}/label`) ?? issueText(issues, `/slots/${name}`)}>
                          <Input id={`${id}-slot-${name}`} data-pointer={`/slots/${name}/label`} value={slot.label} maxLength={80} onChange={(e) => setDoc({ ...doc, slots: { ...slots, [name]: { ...slot, label: e.target.value } } })} />
                        </Field>
                        <div className="flex flex-wrap items-center gap-2">
                          <Chip>{slot.kind === "var_list" ? "columns" : slot.kind === "var" ? "a column" : "a dimension"}</Chip>
                          <ToggleChip pressed={slot.required !== false} onClick={() => setDoc({ ...doc, slots: { ...slots, [name]: { ...slot, required: slot.required === false } } })}>
                            Required
                          </ToggleChip>
                        </div>
                      </li>
                    ))}
                  </ul>
                </BuilderSection>
              ) : null}

              <BuilderSection n={Object.keys(slots).length ? 3 : 2} title="Try it on a dataset" hint="See what the data looks like after each step, before you save.">
                <StagePreview doc={doc} />
              </BuilderSection>

              <BuilderSection n={Object.keys(slots).length ? 4 : 3} title="Name it" done={Boolean(doc.metadata.title.trim())}>
                <div className="grid gap-5">
                  <MetadataFields value={doc.metadata} onChange={(metadata) => setDoc({ ...doc, metadata })} issues={issues} locked={Boolean(itemId)} titlePlaceholder="Weekly totals, then log" />
                  <ScopeChoice value={scope} onChange={setScope} tenantAllowed={!itemId && can(grants, "create", "tenant_library")} />
                </div>
              </BuilderSection>
            </>
          )}
          {issues?.length ? (
            <section aria-label="What the check found" className="rounded-2xl border bg-card p-4">
              <h2 className="mb-2 text-base font-bold">What the check found</h2>
              <IssueList
                issues={issues}
                onSelect={(i) => {
                  if (view === "yaml") setView("builder");
                  requestAnimationFrame(() => focusPointer(root.current, i.pointer));
                }}
              />
            </section>
          ) : null}
          {validation.isError ? <ErrorNotice error={validation.error} title="The check did not run" /> : null}
          {save.error ? <ErrorNotice error={save.error} title={isStaleRevision(save.error) ? "Someone saved this item after you opened it" : "The preset was not saved"} /> : null}
        </div>

        <aside aria-label="Your steps in words" className="grid content-start gap-5 rounded-3xl bg-block p-6 text-block-foreground lg:sticky lg:top-6">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-xl font-bold">Your steps</h2>
            <Chip tone="block">
              {checking ? <Spinner className="size-3" /> : null}
              {checking ? "Checking…" : errors ? `${errors} ${errors === 1 ? "thing" : "things"} to fix` : "Looks good"}
            </Chip>
          </div>
          {steps.length ? (
            <ol className="grid gap-2">
              {steps.map((s, i) => {
                const entry = catalog.byId.get(splitUseRef(s.use)[0]);
                const asked = Object.values(s.with ?? {}).flatMap((v) => (Array.isArray(v) ? v : [v])).map(slotName).filter((x): x is string => x !== null);
                return (
                  <li key={`${s.id}-${i}`} className="rounded-xl bg-block-raised px-3 py-2">
                    <span className="display font-semibold">
                      {i + 1}. {stepTitle(entry, s.use)}
                    </span>
                    {asked.length ? <span className="block text-xs text-block-muted">Asks for: {asked.map((a) => slots[a]?.label ?? a).join("; ")}</span> : null}
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="text-block-muted">Add a step to start.</p>
          )}
          <Button className={cn(PILL, LIME, "w-full")} disabled={save.isPending || (!dirty && savedKey !== null)} onClick={() => void doSave()}>
            {save.isPending ? <Spinner /> : <Save />}
            {savedKey !== null && !dirty ? "Saved" : savedKey !== null ? "Save a new version" : "Save preset"}
          </Button>
          <p className="text-xs text-block-muted">After you save, the preset shows in Build under "Your prep presets".</p>
        </aside>
      </div>
    </div>
  );
}
