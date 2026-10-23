/**
 * Builder › Setup of a layout 2 draft (v0.17, plan §3.7): the words and the source (as on
 * layout 1), the layout (preset: a switch asks first and says what it removes), the result the
 * dashboard is about (output, unit, the class to report and its threshold), the planner's
 * channels (media planner only) and the panels. Choices come from the source catalog of the
 * saved draft; checks against it show next to the fields.
 */
import { useMemo, useState } from "react";

import { ErrorNotice } from "@/components/common";
import { InlineMessage, TagWord } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, Switch } from "@/components/ui/overlays";
import { Field, Input, Label, NativeSelect } from "@/components/ui/primitives";

import { useDashboardPresets } from "../api";
import type { PresetRef } from "../keys";
import { Section } from "../readout/parts";
import { MAX_TILES, PRESET_KEYS, TILE_KEYS, type DashboardSpecV2, type PresetKey, type SourceCatalog, type SubjectUnit } from "../types";
import { ChatSettingsSection } from "./chat-settings";
import { PanelsEditor } from "./panels-editor";
import { ChannelsSection, SourceSection, WordsSection, type SpecEditorProps } from "./setup";
import { issueAt, TILE_LABEL, toggleTile, type FormIssue } from "./spec-form";
import { catalogIssuesV2, outputLabel, outputOf, PERIODS_V2, PRESET_WORDS, presetSwitchReason, SUBJECT_UNIT_WORD, switchPreset, type PeriodLabelV2 } from "./spec-form-v2";
import { UpgradeResult } from "./upgrade-banner";
import { useBuilderCatalog, type BuilderCatalog } from "./use-builder";
import type { SpecUpdate } from "./use-draft";

type V2Props = Omit<SpecEditorProps, "spec"> & { spec: DashboardSpecV2 };
type V2Change = (f: (s: DashboardSpecV2) => DashboardSpecV2) => void;

/** `onChange` of the layout 2 sections: applies only while the draft is layout 2. */
export function v2Change(onChange: (next: SpecUpdate) => void): V2Change {
  return (f) => onChange((s) => (s.layout_version === 2 ? f(s) : s));
}

/** Where the presets of a source are asked (`:presets`), or `null` before one is chosen. */
export function presetRefOf(source: DashboardSpecV2["source"]): PresetRef | null {
  if (source.kind === "endpoint") return source.alias ? { alias: source.alias } : null;
  return source.model_id && source.version ? { model_id: source.model_id, version: source.version } : null;
}

export function SetupV2({ spec, onChange, issues, grid, readOnly = false }: V2Props) {
  const change = v2Change(onChange);
  const cat = useBuilderCatalog(spec);
  const shown = useMemo(() => [...issues, ...catalogIssuesV2(spec, cat.catalog)], [issues, spec, cat.catalog]);
  return (
    <div className="grid min-w-0 gap-5">
      <UpgradeResult />
      <CatalogState cat={cat} />
      <fieldset disabled={readOnly} className="grid min-w-0 gap-5 xl:grid-cols-2">
        <legend className="sr-only">Setup</legend>
        <WordsSection spec={spec} onChange={onChange} issues={shown} />
        <SourceSection spec={spec} onChange={onChange} issues={shown} grid={grid} />
        <PresetSection spec={spec} onChange={change} grid={grid.channels} />
        <SubjectSection spec={spec} onChange={change} issues={shown} catalog={cat.catalog} />
        {spec.mmm ? <PlannerSection spec={spec} onChange={change} issues={shown} grid={grid} /> : null}
        <PanelsEditor spec={spec} onChange={change} issues={shown} catalog={cat.catalog} readOnly={readOnly} />
        <div className="flex items-start gap-3 xl:col-span-2">
          <Switch id="ds-technical" checked={spec.show_technical ?? false} onCheckedChange={(on) => change((s) => ({ ...s, show_technical: on }))} />
          <span className="grid gap-0.5">
            <Label htmlFor="ds-technical">Let the client open the technical details</Label>
            <span className="text-xs text-muted-foreground">Model version, fit checks and intervals, behind a toggle.</span>
          </span>
        </div>
      </fieldset>
      <ChatSettingsSection />
    </div>
  );
}

function CatalogState({ cat }: { cat: BuilderCatalog }) {
  if (cat.stale)
    return (
      <InlineMessage tone="info" size="s" role="none">
        You changed the source. Save the draft to see what the new model offers to the panels and inputs.
      </InlineMessage>
    );
  if (cat.catalog === null) return <ErrorNotice error={cat.error} title="The app could not read what the model offers" onRetry={cat.retry} />;
  return null;
}

// ------------------------------------------------------------------------------ preset

function PresetSection({ spec, onChange, grid }: { spec: DashboardSpecV2; onChange: V2Change; grid: readonly string[] | null | undefined }) {
  const presets = useDashboardPresets(presetRefOf(spec.source));
  const [to, setTo] = useState<PresetKey | null>(null);
  const suggested = presets.data?.suggestions ?? [];
  const reasonOf = (k: PresetKey): string | null => {
    const why = presetSwitchReason(spec, k, { gridChannels: grid });
    if (why) return why;
    if (presets.isSuccess && k !== spec.preset && !suggested.some((s) => s.preset === k)) return `This model does not fit the ${PRESET_WORDS[k].label.toLowerCase()} layout.`;
    return null;
  };
  const current = suggested.find((s) => s.preset === spec.preset);
  const pending = to ? switchPreset(spec, to, { gridChannels: grid }) : null;
  return (
    <Section title="Layout" description="The kind of readout. Each layout has its own panels and inputs for clients.">
      <div className="grid gap-1">
        <p className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{PRESET_WORDS[spec.preset].label}</span>
          {suggested[0]?.preset === spec.preset ? <TagWord tone="positive">Suggested for this model</TagWord> : null}
        </p>
        <p className="text-sm text-muted-foreground">{PRESET_WORDS[spec.preset].hint}</p>
        {current?.reasons?.length ? <p className="text-xs text-muted-foreground">{current.reasons.join(" ")}</p> : null}
        {current?.blocked_inputs?.length ? <p className="text-xs text-muted-foreground">Not possible with this model: {current.blocked_inputs.join("; ")}.</p> : null}
      </div>
      <Field label="Change the layout" htmlFor="ds-preset" hint="You confirm before anything changes.">
        <NativeSelect id="ds-preset" value={spec.preset} onChange={(e) => setTo(e.target.value as PresetKey)}>
          {PRESET_KEYS.map((k) => {
            const why = k === spec.preset ? null : reasonOf(k);
            return (
              <option key={k} value={k} disabled={Boolean(why)}>
                {PRESET_WORDS[k].label}
                {why ? ` (${why.replace(/\.$/, "")})` : ""}
              </option>
            );
          })}
        </NativeSelect>
      </Field>
      {to && pending && to !== spec.preset ? (
        <Dialog open onOpenChange={(o) => !o && setTo(null)}>
          <DialogContent>
            <DialogTitle>Use the {PRESET_WORDS[to].label.toLowerCase()} layout?</DialogTitle>
            <DialogDescription>{PRESET_WORDS[to].hint}</DialogDescription>
            {pending.removed.length ? (
              <div className="grid gap-1 text-sm">
                <p className="font-medium">This removes or turns off:</p>
                <ul aria-label="What the change removes" className="grid list-disc gap-0.5 pl-5">
                  {pending.removed.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-sm">The panels and inputs stay as they are.</p>
            )}
            <p className="text-sm text-muted-foreground">Nothing is saved before you save the draft.</p>
            <DialogFooter>
              <Button variant="ghost" onClick={() => setTo(null)}>
                Keep the {PRESET_WORDS[spec.preset].label.toLowerCase()} layout
              </Button>
              <Button
                onClick={() => {
                  onChange((s) => switchPreset(s, to, { gridChannels: grid }).spec);
                  setTo(null);
                }}
              >
                Change the layout
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </Section>
  );
}

// ------------------------------------------------------------------------------ subject

const UNITS_V2: SubjectUnit[] = ["number", "count", "currency", "ratio", "percent", "probability", "rate", "index", "label"];

function SubjectSection({ spec, onChange, issues, catalog }: { spec: DashboardSpecV2; onChange: V2Change; issues: readonly FormIssue[]; catalog: SourceCatalog | null | undefined }) {
  const s = spec.subject;
  const set = (patch: Partial<DashboardSpecV2["subject"]>) => onChange((x) => ({ ...x, subject: { ...x.subject, ...patch } }));
  const o = outputOf(catalog, s.variable);
  const classes = o?.classes ?? [];
  const classifier = spec.preset === "classifier" || s.unit === "probability" || o?.meaning === "class_probability";
  const outs = catalog?.outputs ?? [];
  return (
    <Section title="Result" description="What the dashboard is about, and how its numbers read.">
      <div className="grid gap-4 sm:grid-cols-2">
        {outs.length ? (
          <Field label="Model output" htmlFor="ds-subject-var" error={issueAt(issues, "/subject/variable")}>
            <NativeSelect id="ds-subject-var" value={s.variable} onChange={(e) => set({ variable: e.target.value })}>
              {o ? null : <option value={s.variable}>{s.variable}</option>}
              {outs.map((x) => (
                <option key={x.name} value={x.name}>
                  {outputLabel(x.name)} ({x.name})
                </option>
              ))}
            </NativeSelect>
          </Field>
        ) : (
          <Field label="Model output" htmlFor="ds-subject-var" error={issueAt(issues, "/subject/variable")}>
            <Input id="ds-subject-var" className="font-mono" value={s.variable} onChange={(e) => set({ variable: e.target.value.trim() })} />
          </Field>
        )}
        <Field label="Label" htmlFor="ds-subject-label" error={issueAt(issues, "/subject/label")} hint="For example: Sales, Chance of churn.">
          <Input id="ds-subject-label" maxLength={60} value={s.label} onChange={(e) => set({ label: e.target.value })} />
        </Field>
        <Field label="Unit" htmlFor="ds-subject-unit">
          <NativeSelect id="ds-subject-unit" value={s.unit} onChange={(e) => set({ unit: e.target.value as SubjectUnit, currency: e.target.value === "currency" ? (s.currency ?? "EUR") : null })}>
            {UNITS_V2.map((u) => (
              <option key={u} value={u}>
                {SUBJECT_UNIT_WORD[u]}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {s.unit === "currency" ? (
          <Field label="Currency" htmlFor="ds-subject-cur" error={issueAt(issues, "/subject/currency")} hint="A code of 3 letters, for example EUR.">
            <Input id="ds-subject-cur" maxLength={3} className="uppercase" value={s.currency ?? ""} onChange={(e) => set({ currency: e.target.value.toUpperCase() })} />
          </Field>
        ) : null}
        <Field label="Decimals" htmlFor="ds-subject-dec" error={issueAt(issues, "/subject/decimals")}>
          <Input id="ds-subject-dec" type="number" min={0} max={6} step={1} value={s.decimals ?? 0} onChange={(e) => set({ decimals: Number(e.target.value) })} />
        </Field>
        <Field label="Readouts are for each" htmlFor="ds-period">
          <NativeSelect id="ds-period" value={spec.period_label ?? "period"} onChange={(e) => onChange((x) => ({ ...x, period_label: e.target.value as PeriodLabelV2 }))}>
            {PERIODS_V2.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {classifier ? (
          <>
            {classes.length ? (
              <Field label="Class to report" htmlFor="ds-subject-pos" error={issueAt(issues, "/subject/positive_label")} hint="The dashboard shows the chance of this class.">
                <NativeSelect id="ds-subject-pos" value={s.positive_label ?? ""} onChange={(e) => set({ positive_label: e.target.value || null })}>
                  <option value="">The most likely class</option>
                  {classes.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
            ) : (
              <Field label="Class to report (optional)" htmlFor="ds-subject-pos" error={issueAt(issues, "/subject/positive_label")} hint="The class name as the model writes it, for example churned.">
                <Input id="ds-subject-pos" maxLength={64} value={s.positive_label ?? ""} onChange={(e) => set({ positive_label: e.target.value || null })} />
              </Field>
            )}
            <Field
              label="Threshold"
              htmlFor="ds-threshold"
              error={issueAt(issues, "/actuals/threshold")}
              hint={`A case counts as ${s.positive_label ? `“${s.positive_label}”` : "positive"} when its chance is above this. From 0.05 to 0.95. Clients cannot change it.`}
            >
              <Input
                id="ds-threshold"
                type="number"
                min={0.05}
                max={0.95}
                step={0.05}
                value={spec.actuals?.threshold ?? 0.5}
                onChange={(e) => onChange((x) => ({ ...x, actuals: { ...x.actuals, threshold: Number(e.target.value) } }))}
              />
            </Field>
          </>
        ) : null}
        {spec.preset === "mmm_planner" && s.unit === "count" ? (
          <Field label="Value of one unit (optional)" htmlFor="ds-subject-vpu" error={issueAt(issues, "/subject/value_per_unit")} hint="In the spend's currency. With it, the dashboard shows where each channel's spend sits.">
            <Input id="ds-subject-vpu" type="number" min={0} step="any" value={s.value_per_unit ?? ""} onChange={(e) => set({ value_per_unit: e.target.value === "" ? null : Number(e.target.value) })} />
          </Field>
        ) : null}
      </div>
    </Section>
  );
}

// ------------------------------------------------------------------------------ planner

function PlannerSection({ spec, onChange, issues, grid }: { spec: DashboardSpecV2; onChange: V2Change; issues: readonly FormIssue[]; grid: SpecEditorProps["grid"] }) {
  const tiles = spec.mmm?.tiles ?? [];
  const setMmm = (f: (m: NonNullable<DashboardSpecV2["mmm"]>) => NonNullable<DashboardSpecV2["mmm"]>) => onChange((s) => (s.mmm ? { ...s, mmm: f(s.mmm) } : s));
  return (
    <>
      <Section title="Planner tiles" description={`Up to ${MAX_TILES} tiles in the planner's “This period” part.`}>
        <fieldset className="grid gap-2">
          <legend className="sr-only">Tiles</legend>
          {TILE_KEYS.map((t) => {
            const on = tiles.includes(t);
            return (
              <label key={t} className="flex items-start gap-2 text-sm">
                <Checkbox className="mt-0.5" checked={on} disabled={!on && tiles.length >= MAX_TILES} onCheckedChange={() => setMmm((m) => ({ ...m, tiles: toggleTile(m.tiles ?? [], t) }))} />
                <span className="grid gap-0.5">
                  <span className="font-medium">{TILE_LABEL[t].label}</span>
                  <span className="text-xs text-muted-foreground">{TILE_LABEL[t].hint}</span>
                </span>
              </label>
            );
          })}
          {issueAt(issues, "/mmm/tiles") ? (
            <p role="alert" className="text-xs font-medium text-destructive">
              {issueAt(issues, "/mmm/tiles")}
            </p>
          ) : null}
        </fieldset>
      </Section>
      <ChannelsSection className="xl:col-span-2" at="/mmm/channels" channels={spec.mmm?.channels ?? []} onChannels={(f) => setMmm((m) => ({ ...m, channels: f(m.channels) }))} issues={issues} grid={grid} />
    </>
  );
}
