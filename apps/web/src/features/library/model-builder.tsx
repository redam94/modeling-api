/**
 * Make your own › Models: the model recipe builder (v0.18 D3, D4, §3.4).
 *
 * Two ways to make a model recipe:
 * - **Describe a model** (`base: declarative`): pick a dataset, say what you want to explain
 *   and what kind of number it is, add the drivers (transform chips, a prior picker with a live
 *   density sparkline, the direction of the effect, whether it varies by a group), pick the
 *   groups, then review it in words or as YAML. It compiles to `tabular.declarative_glm`.
 * - **Preset a catalog model** (`base: <id>@<range>`): pick a model, set it up with its own
 *   settings form (sanitized schema), and choose which settings people may change when they use it.
 *
 * Every change is checked by `POST /v1/library:validate` (300 ms after you stop typing); each
 * problem is shown at the field its pointer names. Save, Use in Build and Fit now live in the
 * review block (the page's one ink block). Everything is reachable with the keyboard: add a
 * driver, move it up or down, remove it, save.
 */
import { ArrowDown, ArrowUp, Hammer, Play, Plus, Save, Trash2 } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";

import { useCatalog } from "@/api/queries";
import { can } from "@/api/access";
import type { Issue } from "@/api/types";
import { ErrorNotice, IssueList, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { SchemaForm, type JsonObject as FormObject } from "@/features/forms/json-schema-form";
import { safeSchema } from "@/features/forms/schema-sanitize";
import { plainIssue } from "@/lib/plain-issue";
import { cn } from "@/lib/utils";

import { useModelRecipeUse, useSaveDoc, type Saved } from "./actions";
import { useModelCatalog } from "./catalog";
import { docKey, isStaleRevision, useLibraryItem, useLibraryValidation } from "./api";
import { DatasetPicker, isGroupLike, isNumeric, useDatasetColumns, type ColumnInfo } from "./data-source";
import { FAMILY_OPTIONS, isDeclarativeDoc, moveItem, newTerm, SIGN_OPTIONS, summarizeModelRecipe, tidyDoc, TRANSFORM_OPTIONS } from "./doc";
import { PriorPicker } from "./prior-picker";
import { BuilderSection, Chip, focusPointer, IssueLines, issueText, LIME, MetadataFields, PILL, PILL_SM, ScopeChoice, ToggleChip, useDebounced } from "./ui";
import { LazyYamlTab as YamlTab } from "./yaml-tab-lazy";
import {
  hasErrors,
  isModelRecipeDoc,
  MAX_EXPOSED,
  MAX_GROUPS,
  MAX_TERMS,
  MAX_TRANSFORMS_PER_TERM,
  POSITIVE_DISTS,
  splitUseRef,
  type DeclarativeModel,
  type JsonObject,
  type LibraryDoc,
  type LibraryScope,
  type ModelRecipeDoc,
  type Sign,
  type TermSpec,
  type TransformUse,
} from "./types";

const VALIDATE_DEBOUNCE_MS = 300;
const P = "/declarative";

// ------------------------------------------------------------------------------ small helpers

function VarInput({ id, value, onChange, columns, pointer, placeholder, filter, exclude = [] }: { id: string; value: string; onChange: (v: string) => void; columns: ColumnInfo[]; pointer: string; placeholder: string; filter: (c: ColumnInfo) => boolean; exclude?: string[] }) {
  const options = columns.filter(filter).filter((c) => c.name === value || !exclude.includes(c.name));
  if (!columns.length) {
    return <Input id={id} data-pointer={pointer} className="font-mono text-xs" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value.trim())} />;
  }
  return (
    <NativeSelect id={id} data-pointer={pointer} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{placeholder}</option>
      {value && !options.some((c) => c.name === value) ? <option value={value}>{value} (not in this dataset)</option> : null}
      {options.map((c) => (
        <option key={`${c.kind}-${c.name}`} value={c.name}>
          {c.name}
          {c.role ? ` (${c.role})` : c.kind === "dim" ? " (dimension)" : ""}
        </option>
      ))}
    </NativeSelect>
  );
}

function focusLater(selector: string) {
  requestAnimationFrame(() => document.querySelector<HTMLElement>(selector)?.focus());
}

// ------------------------------------------------------------------------------ one driver

function TermCard({
  term,
  index,
  count,
  issues,
  columns,
  groups,
  usedVars,
  outcome,
  onChange,
  onMove,
  onRemove,
}: {
  term: TermSpec;
  index: number;
  count: number;
  issues: readonly Issue[] | undefined;
  columns: ColumnInfo[];
  groups: string[];
  usedVars: string[];
  outcome: string;
  onChange: (t: TermSpec) => void;
  onMove: (to: number) => void;
  onRemove: () => void;
}) {
  const id = useId();
  const ptr = `${P}/terms/${index}`;
  const name = term.label?.trim() || term.var || `Driver ${index + 1}`;
  const transforms = term.transform ?? [];
  const positivePrior = POSITIVE_DISTS.has(term.prior?.dist ?? "normal");
  const sign: Sign = term.sign ?? "any";
  const toggleTransform = (use: TransformUse) => {
    const has = transforms.some((t) => t.use === use);
    if (has) onChange({ ...term, transform: transforms.filter((t) => t.use !== use) });
    else if (transforms.length < MAX_TRANSFORMS_PER_TERM) onChange({ ...term, transform: [...transforms, { use }] });
  };
  return (
    <li className="rounded-2xl border bg-background p-4 sm:p-5" data-term={term.var || `#${index}`} aria-label={`Driver ${index + 1}: ${name}`}>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-lg leading-tight font-bold [overflow-wrap:anywhere]">{name}</h3>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon-sm" data-action="up" aria-label={`Move ${name} up`} disabled={index === 0} onClick={() => onMove(index - 1)}>
            <ArrowUp />
          </Button>
          <Button variant="ghost" size="icon-sm" data-action="down" aria-label={`Move ${name} down`} disabled={index === count - 1} onClick={() => onMove(index + 1)}>
            <ArrowDown />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label={`Remove ${name}`} onClick={onRemove}>
            <Trash2 />
          </Button>
        </div>
      </div>
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="grid content-start gap-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Column" htmlFor={`${id}-var`} error={issueText(issues, `${ptr}/var`)}>
              <VarInput id={`${id}-var`} pointer={`${ptr}/var`} value={term.var} columns={columns} placeholder="Pick a column" filter={isNumeric} exclude={[...usedVars, outcome]} onChange={(v) => onChange({ ...term, var: v })} />
            </Field>
            <Field label="Label (optional)" htmlFor={`${id}-label`} error={issueText(issues, `${ptr}/label`)}>
              <Input id={`${id}-label`} data-pointer={`${ptr}/label`} value={term.label ?? ""} maxLength={64} placeholder="Media spend" onChange={(e) => onChange({ ...term, label: e.target.value })} />
            </Field>
          </div>
          <div className="grid gap-2" data-pointer={`${ptr}/transform`}>
            <span className="text-sm font-medium">Shape it first (up to {MAX_TRANSFORMS_PER_TERM}, in this order)</span>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={`Transforms of ${name}`}>
              {TRANSFORM_OPTIONS.map((o) => {
                const at = transforms.findIndex((t) => t.use === o.value);
                return (
                  <ToggleChip key={o.value} pressed={at >= 0} title={o.hint} disabled={at < 0 && transforms.length >= MAX_TRANSFORMS_PER_TERM} onClick={() => toggleTransform(o.value)}>
                    {at >= 0 ? <span className="tabular opacity-70">{at + 1}</span> : null}
                    {o.label}
                  </ToggleChip>
                );
              })}
            </div>
            <IssueLines issues={issues} pointer={`${ptr}/transform`} />
          </div>
          <div className="grid gap-2" data-pointer={`${ptr}/sign`}>
            <span className="text-sm font-medium">Which way can it move the outcome?</span>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={`Direction of ${name}`}>
              {SIGN_OPTIONS.map((o) => (
                <ToggleChip key={o.value} pressed={sign === o.value} disabled={positivePrior && o.value === "any"} onClick={() => onChange({ ...term, sign: o.value })}>
                  {o.label}
                </ToggleChip>
              ))}
            </div>
            <IssueLines issues={issues} pointer={`${ptr}/sign`} />
          </div>
          <Field label="Does the effect differ by group?" htmlFor={`${id}-vary`} hint={groups.length ? undefined : "Add a group below to let the effect differ between groups."} error={issueText(issues, `${ptr}/varies_by`)}>
            <NativeSelect id={`${id}-vary`} data-pointer={`${ptr}/varies_by`} value={term.varies_by ?? ""} disabled={!groups.length && !term.varies_by} onChange={(e) => onChange({ ...term, varies_by: e.target.value || null })}>
              <option value="">The same everywhere</option>
              {[...new Set([...groups, ...(term.varies_by ? [term.varies_by] : [])])].map((g) => (
                <option key={g} value={g}>
                  Differs by {g}
                </option>
              ))}
            </NativeSelect>
          </Field>
        </div>
        <div data-tour={index === 0 ? "library.prior" : undefined}>
          <PriorPicker value={term.prior} sign={sign} pointer={`${ptr}/prior`} issues={issues} label={name} onChange={(prior) => onChange({ ...term, prior, ...(POSITIVE_DISTS.has(prior.dist ?? "normal") && sign === "any" ? { sign: "positive" as const } : {}) })} />
        </div>
      </div>
    </li>
  );
}

// ------------------------------------------------------------------------------ declarative parts

function DeclarativeSections({ model, onModel, issues, columns, datasetPicked }: { model: DeclarativeModel; onModel: (m: DeclarativeModel) => void; issues: readonly Issue[] | undefined; columns: ColumnInfo[]; datasetPicked: boolean }) {
  const id = useId();
  const [adding, setAdding] = useState("");
  const terms = model.terms ?? [];
  const groups = (model.groups ?? []).map((g) => g.dim);
  const usedVars = terms.map((t) => t.var).filter(Boolean);
  const outcome = model.outcome?.var ?? "";
  const family = model.outcome?.family ?? "normal";
  const setTerms = (next: TermSpec[]) => onModel({ ...model, terms: next });
  const freeDrivers = columns.filter(isNumeric).filter((c) => c.name !== outcome && !usedVars.includes(c.name));
  const groupCandidates = columns.filter(isGroupLike).filter((c) => c.name !== outcome);

  const addTerm = () => {
    if (terms.length >= MAX_TERMS) return;
    const variable = adding || freeDrivers[0]?.name || "";
    setTerms([...terms, newTerm(variable)]);
    setAdding("");
    focusLater(`[data-term="${variable || `#${terms.length}`}"] select, [data-term="${variable || `#${terms.length}`}"] input`);
  };
  const move = (from: number, to: number) => {
    const key = terms[from]?.var || `#${to}`;
    setTerms(moveItem(terms, from, to));
    focusLater(`[data-term="${key}"] [data-action="${to < from ? "up" : "down"}"]:not(:disabled), [data-term="${key}"] [data-action]`);
  };

  return (
    <>
      <BuilderSection n={2} title="What do you want to explain?" hint="Pick the column the model explains, then say what kind of number it is." done={Boolean(outcome)}>
        <div className="grid gap-5">
          <Field label="The outcome" htmlFor={`${id}-outcome`} hint={datasetPicked ? undefined : "Pick a dataset above to choose from its columns, or type a column name."} error={issueText(issues, `${P}/outcome/var`)} className="max-w-md">
            <VarInput id={`${id}-outcome`} pointer={`${P}/outcome/var`} value={outcome} columns={columns} placeholder="Pick the outcome" filter={isNumeric} exclude={usedVars} onChange={(v) => onModel({ ...model, outcome: { ...model.outcome, var: v } })} />
          </Field>
          <fieldset className="grid gap-2" data-pointer={`${P}/outcome/family`}>
            <legend className="mb-2 text-sm font-medium">It is…</legend>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3" role="group" aria-label="What kind of number the outcome is">
              {FAMILY_OPTIONS.map((f) => {
                const on = family === f.value;
                return (
                  <button
                    key={f.value}
                    type="button"
                    aria-pressed={on}
                    onClick={() => onModel({ ...model, outcome: { ...model.outcome, family: f.value, ...(f.value === "ordinal" ? { n_categories: model.outcome.n_categories ?? 5 } : { n_categories: null }) } })}
                    className={cn(
                      "focus-ring grid gap-1 rounded-xl border-[1.5px] p-3 text-left transition-colors",
                      on ? "border-primary bg-accent" : "border-transparent bg-background hover:border-border-strong",
                    )}
                  >
                    <span className="display text-base font-bold">{f.label}</span>
                    <span className="text-xs text-muted-foreground">{f.hint}</span>
                  </button>
                );
              })}
            </div>
            <IssueLines issues={issues} pointer={`${P}/outcome/family`} />
          </fieldset>
          {family === "ordinal" ? (
            <Field label="How many rating levels?" htmlFor={`${id}-ncat`} error={issueText(issues, `${P}/outcome/n_categories`)} className="max-w-48">
              <Input id={`${id}-ncat`} data-pointer={`${P}/outcome/n_categories`} type="number" min={2} max={50} value={model.outcome.n_categories ?? 5} onChange={(e) => onModel({ ...model, outcome: { ...model.outcome, n_categories: Math.trunc(Number(e.target.value)) || 2 } })} />
            </Field>
          ) : null}
        </div>
      </BuilderSection>

      <BuilderSection n={3} title="What drives it?" hint="Add the things you think move the outcome. For each one, say how sure you are about its effect before seeing the data." done={terms.length > 0}>
        {terms.length ? (
          <ol className="grid gap-3" aria-label="Drivers">
            {terms.map((t, i) => (
              <TermCard
                key={`${t.var}-${i}`}
                term={t}
                index={i}
                count={terms.length}
                issues={issues}
                columns={columns}
                groups={groups}
                usedVars={usedVars.filter((v) => v !== t.var)}
                outcome={outcome}
                onChange={(next) => setTerms(terms.map((x, j) => (j === i ? next : x)))}
                onMove={(to) => move(i, to)}
                onRemove={() => {
                  setTerms(terms.filter((_, j) => j !== i));
                  focusLater(`#${CSS.escape(`${id}-add`)}`);
                }}
              />
            ))}
          </ol>
        ) : (
          <p className="rounded-xl border border-dashed border-border-strong p-4 text-muted-foreground">No drivers yet. Add your first one: price, promo, media spend, the weather…</p>
        )}
        <IssueLines issues={issues} pointer={`${P}/terms`} className="mt-2" />
        {/* v0.18 live: the tour's "prior" step points at the first driver's prior; a new
            recipe has no driver yet, so it points at adding one instead of "not on screen" */}
        <div className="mt-4 flex flex-wrap items-end gap-2" data-pointer={`${P}/terms`} data-tour={terms.length ? undefined : "library.prior"}>
          {columns.length ? (
            <Field label="Add a driver" htmlFor={`${id}-add`} className="min-w-48 flex-1 sm:max-w-xs">
              <NativeSelect id={`${id}-add`} value={adding} onChange={(e) => setAdding(e.target.value)}>
                <option value="">{freeDrivers.length ? "Pick a column" : "Every numeric column is in use"}</option>
                {freeDrivers.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          ) : null}
          <Button id={columns.length ? undefined : `${id}-add`} variant="secondary" className={PILL} onClick={addTerm} disabled={terms.length >= MAX_TERMS || (columns.length > 0 && !adding && !freeDrivers.length)}>
            <Plus /> Add a driver
          </Button>
        </div>
      </BuilderSection>

      <BuilderSection n={4} title="Does it differ by group?" hint={`Markets, stores or regions often start from different levels. Each group you add gets its own starting level (up to ${MAX_GROUPS}).`} done={groups.length > 0}>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Groups" data-pointer={`${P}/groups`}>
          {[...new Set([...groupCandidates.map((c) => c.name), ...groups])].map((g) => {
            const on = groups.includes(g);
            return (
              <ToggleChip
                key={g}
                pressed={on}
                disabled={!on && groups.length >= MAX_GROUPS}
                onClick={() => {
                  if (on) {
                    onModel({ ...model, groups: (model.groups ?? []).filter((x) => x.dim !== g), terms: terms.map((t) => (t.varies_by === g ? { ...t, varies_by: null } : t)) });
                  } else onModel({ ...model, groups: [...(model.groups ?? []), { dim: g }] });
                }}
              >
                {g}
              </ToggleChip>
            );
          })}
          {!groupCandidates.length && !groups.length ? <p className="text-muted-foreground">{datasetPicked ? "This dataset has no column that names a group." : "Pick a dataset to see the columns that can be groups."}</p> : null}
        </div>
        <IssueLines issues={issues} pointer={`${P}/groups`} className="mt-2" />
        {groups.length ? (
          <details className="mt-4 rounded-xl border p-3">
            <summary className="cursor-pointer text-sm font-medium">How much do groups differ? (priors)</summary>
            <div className="mt-3 grid gap-5 md:grid-cols-2">
              {(model.groups ?? []).map((g, i) => (
                <PriorPicker key={g.dim} positiveOnly label={`Spread between ${g.dim}`} pointer={`${P}/groups/${i}/sigma_prior`} issues={issues} value={g.sigma_prior ?? { dist: "half_normal", sigma: 1 }} sign="positive" onChange={(p) => onModel({ ...model, groups: (model.groups ?? []).map((x, j) => (j === i ? { ...x, sigma_prior: p } : x)) })} />
              ))}
            </div>
          </details>
        ) : null}
      </BuilderSection>

      <details className="rounded-2xl border bg-card p-5">
        <summary className="cursor-pointer text-base font-semibold">More: interactions, baseline and noise</summary>
        <div className="mt-4 grid gap-6">
          <div className="grid gap-3" data-pointer={`${P}/interactions`}>
            <h3 className="text-base font-bold">Drivers that work together</h3>
            <p className="text-muted-foreground">An interaction lets one driver change the effect of another (a promo that works better when the price is low).</p>
            {(model.interactions ?? []).map((x, i) => (
              <div key={i} className="grid items-end gap-3 rounded-xl border p-3 sm:grid-cols-[1fr_1fr_auto]">
                <Field label="This driver" htmlFor={`${id}-ia-${i}`} error={issueText(issues, `${P}/interactions/${i}/a`)}>
                  <NativeSelect id={`${id}-ia-${i}`} data-pointer={`${P}/interactions/${i}/a`} value={x.a} onChange={(e) => onModel({ ...model, interactions: (model.interactions ?? []).map((y, j) => (j === i ? { ...y, a: e.target.value } : y)) })}>
                    <option value="">Pick a driver</option>
                    {usedVars.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
                <Field label="with this one" htmlFor={`${id}-ib-${i}`} error={issueText(issues, `${P}/interactions/${i}/b`)}>
                  <NativeSelect id={`${id}-ib-${i}`} data-pointer={`${P}/interactions/${i}/b`} value={x.b} onChange={(e) => onModel({ ...model, interactions: (model.interactions ?? []).map((y, j) => (j === i ? { ...y, b: e.target.value } : y)) })}>
                    <option value="">Pick a driver</option>
                    {usedVars.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
                <Button variant="ghost" size="icon" aria-label={`Remove interaction ${i + 1}`} onClick={() => onModel({ ...model, interactions: (model.interactions ?? []).filter((_, j) => j !== i) })}>
                  <Trash2 />
                </Button>
              </div>
            ))}
            <div>
              <Button variant="secondary" className={PILL_SM} disabled={usedVars.length < 2 || (model.interactions ?? []).length >= 16} onClick={() => onModel({ ...model, interactions: [...(model.interactions ?? []), { a: usedVars[0] ?? "", b: usedVars[1] ?? "" }] })}>
                <Plus /> Add an interaction
              </Button>
            </div>
            <IssueLines issues={issues} pointer={`${P}/interactions`} />
          </div>
          <div className="grid gap-6 md:grid-cols-2">
            <PriorPicker label="Baseline (intercept)" pointer={`${P}/intercept`} issues={issues} value={model.intercept ?? { dist: "normal", sigma: 2 }} onChange={(p) => onModel({ ...model, intercept: p })} />
            <PriorPicker positiveOnly label="Noise" pointer={`${P}/noise`} issues={issues} value={model.noise ?? { dist: "half_normal", sigma: 1 }} sign="positive" onChange={(p) => onModel({ ...model, noise: p })} />
          </div>
        </div>
      </details>
    </>
  );
}

// ------------------------------------------------------------------------------ preset parts

function settingsErrors(issues: readonly Issue[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const i of issues ?? []) {
    const p = i.pointer ?? "";
    if (p.startsWith("/settings/") && (i.severity ?? "error") !== "info") {
      const key = p.slice("/settings/".length).split("/")[0] ?? "";
      out[key] ??= plainIssue(i);
    }
  }
  return out;
}

function PresetSections({ doc, onDoc, issues }: { doc: ModelRecipeDoc; onDoc: (d: ModelRecipeDoc) => void; issues: readonly Issue[] | undefined }) {
  const id = useId();
  const { models, isPending: catalogPending } = useModelCatalog();
  const [baseId] = splitUseRef(doc.base ?? "");
  const entry = models.find((m) => m.manifest.id === baseId);
  const schema = useMemo(() => safeSchema((entry?.manifest.settings_schema ?? {}) as FormObject), [entry]);
  const keys = Object.keys((schema.properties as FormObject | undefined) ?? {});
  const exposed = doc.exposed ?? [];
  return (
    <>
      <BuilderSection n={1} title="Pick a model from the catalog" hint="Your preset keeps your favourite settings for it. The model itself does not change." done={Boolean(entry)}>
        <Field label="Model" htmlFor={`${id}-base`} error={issueText(issues, "/base")} className="max-w-lg">
          <NativeSelect id={`${id}-base`} data-pointer="/base" value={baseId} disabled={catalogPending} onChange={(e) => onDoc({ ...doc, base: e.target.value ? `${e.target.value}@^${(models.find((m) => m.manifest.id === e.target.value)?.manifest.version ?? "1").split(".")[0]}` : "", settings: {}, exposed: [] })}>
            <option value="">{catalogPending ? "Loading the catalog…" : "Pick a model"}</option>
            {baseId && !entry ? <option value={baseId}>{baseId} (not in this catalog)</option> : null}
            {models.map((m) => (
              <option key={m.manifest.id} value={m.manifest.id}>
                {m.manifest.about?.title ? `${m.manifest.about.title} (${m.manifest.id})` : m.manifest.id}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {entry?.manifest.about?.summary_plain ? <p className="mt-3 max-w-[70ch] text-muted-foreground">{entry.manifest.about.summary_plain}</p> : null}
      </BuilderSection>
      {entry ? (
        <>
          <BuilderSection n={2} title="Set it up your way" hint="Leave a field empty to keep the model's default.">
            <SchemaForm schema={schema} value={(doc.settings ?? {})} onChange={(settings) => onDoc({ ...doc, settings: settings })} errors={settingsErrors(issues)} nested />
          </BuilderSection>
          <BuilderSection n={3} title="What can people change when they use it?" hint={`Pick up to ${MAX_EXPOSED} settings. Everything else stays as you set it.`}>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Settings people can change" data-pointer="/exposed">
              {keys.map((k) => {
                const on = exposed.includes(k);
                return (
                  <ToggleChip key={k} pressed={on} disabled={!on && exposed.length >= MAX_EXPOSED} onClick={() => onDoc({ ...doc, exposed: on ? exposed.filter((x) => x !== k) : [...exposed, k] })}>
                    <span className="font-mono">{k}</span>
                  </ToggleChip>
                );
              })}
              {!keys.length ? <p className="text-muted-foreground">This model has no settings to share.</p> : null}
            </div>
            <IssueLines issues={issues} pointer="/exposed" className="mt-2" />
          </BuilderSection>
        </>
      ) : null}
    </>
  );
}

/** The form of a preset's exposed settings only (what people fill in when they use it, D4). */
export function exposedSchema(entrySchema: FormObject, exposed: readonly string[]): FormObject {
  const props = (entrySchema.properties as FormObject | undefined) ?? {};
  return { type: "object", properties: Object.fromEntries(Object.entries(props).filter(([k]) => exposed.includes(k))) };
}

// ------------------------------------------------------------------------------ review block

function ReviewBlock({
  doc,
  checking,
  issues,
  dirty,
  saving,
  saved,
  canUse,
  busy,
  onSave,
  onUse,
  datasetPicked,
}: {
  doc: ModelRecipeDoc;
  checking: boolean;
  issues: readonly Issue[] | undefined;
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  canUse: boolean;
  busy: "build" | "fit" | null;
  onSave: () => void;
  onUse: (t: "build" | "fit") => void;
  datasetPicked: boolean;
}) {
  const errors = (issues ?? []).filter((i) => (i.severity ?? "error") === "error").length;
  const status = checking ? "Checking…" : errors ? `${errors} ${errors === 1 ? "thing" : "things"} to fix` : "Looks good";
  return (
    <aside aria-label="Your model in words" className="grid content-start gap-5 rounded-3xl bg-block p-6 text-block-foreground lg:sticky lg:top-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xl font-bold">Your model, in words</h2>
        <Chip tone="block">
          {checking ? <Spinner className="size-3" /> : null}
          {status}
        </Chip>
      </div>
      <div data-testid="model-recipe-summary" className="grid gap-2 text-base leading-relaxed">
        {summarizeModelRecipe(doc).map((line, i) => (
          <p key={i} className={i === 0 ? "display text-lg font-semibold" : "text-block-muted"}>
            {line}
          </p>
        ))}
      </div>
      <div className="grid gap-2">
        <Button className={cn(PILL, LIME, "w-full")} onClick={onSave} disabled={saving || (!dirty && saved)}>
          {saving ? <Spinner /> : <Save />}
          {saved && !dirty ? "Saved" : saved ? "Save a new version" : "Save model"}
        </Button>
        <div className="grid grid-cols-2 gap-2" data-tour="library.use">
          <Button variant="ghost" className={cn(PILL_SM, "text-block-foreground shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised hover:text-block-foreground")} disabled={!canUse || busy !== null} onClick={() => onUse("build")}>
            {busy === "build" ? <Spinner /> : <Hammer />} Use in Build
          </Button>
          <Button variant="ghost" className={cn(PILL_SM, "text-block-foreground shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised hover:text-block-foreground")} disabled={!canUse || busy !== null} onClick={() => onUse("fit")}>
            {busy === "fit" ? <Spinner /> : <Play />} Fit now
          </Button>
        </div>
        <p className="text-xs text-block-muted">{datasetPicked ? (errors ? "Fix the things above to use it." : "Use and Fit save your changes first.") : "Pick a dataset to use it in Build or fit it now."}</p>
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------------------ the builder

export interface ModelBuilderProps {
  /** The saved item being edited (`null` for a new one). */
  itemId: string | null;
  initial: ModelRecipeDoc;
  /** A new item was created (the page puts its id in the URL). */
  onCreated?: (saved: Saved) => void;
}

export function ModelBuilder({ itemId, initial, onCreated }: ModelBuilderProps) {
  const grants = useGrants();
  const item = useLibraryItem(itemId ?? undefined);
  const [doc, setDoc] = useState<ModelRecipeDoc>(initial);
  const [dataset, setDataset] = useState<string | null>(null);
  const [scope, setScope] = useState<LibraryScope>("project");
  const [view, setView] = useState<"builder" | "yaml">("builder");
  const [savedKey, setSavedKey] = useState<string | null>(itemId ? docKey(tidyDoc(initial)) : null);
  const [exposedValues, setExposedValues] = useState<FormObject>({});
  const root = useRef<HTMLDivElement>(null);
  const data = useDatasetColumns(dataset);
  const save = useSaveDoc(item.data);
  const use = useModelRecipeUse();
  const catalog = useCatalog();

  const tidy = useMemo(() => tidyDoc(doc), [doc]);
  const debounced = useDebounced(tidy, VALIDATE_DEBOUNCE_MS);
  const validation = useLibraryValidation(debounced as unknown as JsonObject, data.columns.length ? dataset : null);
  const issues = validation.data?.issues;
  const checking = validation.isFetching || docKey(debounced) !== docKey(tidy);
  const dirty = docKey(tidy) !== savedKey;
  const declarative = isDeclarativeDoc(doc);
  const tenantAllowed = !itemId && can(grants, "create", "tenant_library");

  const doSave = async (): Promise<Saved | null> => {
    try {
      const saved = await save.save(doc, { scope });
      setSavedKey(docKey(tidy));
      if (!itemId) onCreated?.(saved);
      return saved;
    } catch (e) {
      // A stale `If-Match`: load the item's new etag, so the next save can go through.
      if (isStaleRevision(e)) void item.refetch();
      return null;
    }
  };

  const doUse = async (target: "build" | "fit") => {
    if (!dataset) return;
    let id = itemId;
    let revision = item.data?.latest_revision ?? 1;
    if (dirty || !id) {
      const saved = await doSave();
      if (!saved) return;
      id = saved.id;
      revision = saved.revision;
    }
    await use.run({ itemId: id, revision, name: doc.metadata.name, dataset, target, exposed: exposedValues });
  };

  const [baseId] = splitUseRef(doc.base ?? "");
  const presetEntry = catalog.data?.models?.find((m) => m.manifest.id === baseId);
  const blocking = hasErrors(issues);

  return (
    <div ref={root} className="grid gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SegmentedControl
          label="How to edit"
          value={view}
          onValueChange={setView}
          options={[
            { value: "builder", label: "Builder" },
            { value: "yaml", label: "YAML" },
          ]}
        />
        {!itemId ? (
          <SegmentedControl
            label="Kind of model recipe"
            value={declarative ? "declarative" : "preset"}
            onValueChange={(v) => setDoc(v === "declarative" ? { ...doc, base: "declarative", declarative: doc.declarative ?? { outcome: { var: "", family: "normal" }, terms: [] }, settings: undefined, exposed: undefined } : { ...doc, base: "", declarative: undefined, settings: {}, exposed: [] })}
            options={[
              { value: "declarative", label: "Describe a model" },
              { value: "preset", label: "Preset a catalog model" },
            ]}
          />
        ) : null}
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)]">
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] content-start gap-5">
          {view === "yaml" ? (
            <YamlTab kind="model_recipe" doc={doc} issues={issues} onDoc={(d: LibraryDoc) => isModelRecipeDoc(d) && setDoc(d)} />
          ) : (
            <>
              {declarative ? (
                <>
                  <BuilderSection n={1} title="Pick your data" hint="The builder offers the columns of this dataset and checks your model against it. You can change it later." done={Boolean(dataset)}>
                    <div className="max-w-lg">
                      <DatasetPicker value={dataset} onChange={setDataset} />
                    </div>
                    {data.empty ? <p className="mt-2 text-sm text-warning">This dataset has no ready version yet. You can still type column names.</p> : null}
                  </BuilderSection>
                  <DeclarativeSections model={doc.declarative ?? { outcome: { var: "" }, terms: [] }} onModel={(m) => setDoc({ ...doc, declarative: m })} issues={issues} columns={data.columns} datasetPicked={Boolean(dataset)} />
                </>
              ) : (
                <>
                  <PresetSections doc={doc} onDoc={setDoc} issues={issues} />
                  <BuilderSection n={4} title="Pick your data" hint="Needed to use it in Build or fit it now." done={Boolean(dataset)}>
                    <div className="max-w-lg">
                      <DatasetPicker value={dataset} onChange={setDataset} />
                    </div>
                    {presetEntry && (doc.exposed ?? []).length ? (
                      <div className="mt-5 grid gap-3 rounded-xl border p-4">
                        <h3 className="text-base font-bold">When you use it now</h3>
                        <SchemaForm schema={exposedSchema(safeSchema((presetEntry.manifest.settings_schema ?? {}) as FormObject), doc.exposed ?? [])} value={exposedValues} onChange={setExposedValues} nested />
                      </div>
                    ) : null}
                  </BuilderSection>
                </>
              )}
              <BuilderSection n={5} title="Name it" hint="This is what your team sees in Make your own, the Catalog and Build." done={Boolean(doc.metadata.title.trim())}>
                <div className="grid gap-5">
                  <MetadataFields value={doc.metadata} onChange={(metadata) => setDoc({ ...doc, metadata })} issues={issues} locked={Boolean(itemId)} titlePlaceholder="Store sales with promo lift" />
                  <ScopeChoice value={scope} onChange={setScope} tenantAllowed={tenantAllowed} />
                </div>
              </BuilderSection>
            </>
          )}
          {issues?.length ? (
            <section aria-label="What the check found" className="rounded-2xl border bg-card p-4 [overflow-wrap:anywhere]">
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
          {save.error ? (
            <ErrorNotice error={save.error} title={isStaleRevision(save.error) ? "Someone saved this item after you opened it" : "The model was not saved"} />
          ) : null}
          {use.error ? <ErrorNotice error={use.error} title="The model could not be used" /> : null}
        </div>
        <ReviewBlock
          doc={doc}
          checking={checking}
          issues={issues}
          dirty={dirty}
          saving={save.isPending}
          saved={savedKey !== null}
          canUse={Boolean(dataset) && !blocking && !checking}
          busy={use.busy}
          onSave={() => void doSave()}
          onUse={(t) => void doUse(t)}
          datasetPicked={Boolean(dataset)}
        />
      </div>
    </div>
  );
}
