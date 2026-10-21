/**
 * Builder › Setup: the words of the dashboard, its source (an endpoint or a pinned model
 * version), the KPI, the channels (from the response grid), the tiles and the tabs.
 *
 * A layout 2 draft (v0.17: a new dashboard, or one that moved to the new layout) gets the
 * layout 2 setup ({@link SetupV2}: preset, result and panels). A layout 1 draft keeps the v0.16
 * form, with the "Use the new layout" banner above it.
 */
import { Plus, Trash2 } from "lucide-react";

import { useEndpoints } from "@/api/integrations";
import { useModels, useModelVersions } from "@/api/queries";
import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Checkbox, Switch } from "@/components/ui/overlays";
import { Field, Input, Label, NativeSelect, Table, TBody, TD, TH, THead, TR, Textarea } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { Section } from "../readout/parts";
import { isLayout2, MAX_CHANNELS, MAX_TILES, TAB_KEYS, TILE_KEYS, type AnyDashboardSpec, type ChannelDef, type DashboardSource, type DashboardSpec, type KpiDef } from "../types";
import { SetupV2 } from "./setup-v2";
import type { SourceGrid } from "./source";
import {
  channelsFrom,
  issueAt,
  MAX_CLIENT_NAME,
  MAX_LABEL,
  MAX_NOTE,
  MAX_SUMMARY,
  MAX_TITLE,
  TAB_LABEL,
  TILE_LABEL,
  toggleTab,
  toggleTile,
  UNIT_LABEL,
  type FormIssue,
} from "./spec-form";
import type { SpecUpdate } from "./use-draft";

export interface SpecEditorProps {
  spec: AnyDashboardSpec;
  onChange: (next: SpecUpdate) => void;
  issues: readonly FormIssue[];
  grid: SourceGrid;
  readOnly?: boolean;
}

const PERIODS = [
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "period", label: "Period" },
] as const;

const UNITS = ["count", "currency", "ratio", "percent"] as const;

/** A change of a layout 1 draft (ignored once the draft is layout 2). */
export type V1Update = DashboardSpec | ((s: DashboardSpec) => DashboardSpec);

/** `onChange` of the layout 1 sections: applies only while the draft is layout 1. */
export function v1Change(onChange: (next: SpecUpdate) => void): (next: V1Update) => void {
  return (next) => onChange((s) => (isLayout2(s) ? s : typeof next === "function" ? next(s) : next));
}

export function SetupTab({ spec, onChange, issues, grid, readOnly = false }: SpecEditorProps) {
  if (isLayout2(spec)) return <SetupV2 spec={spec} onChange={onChange} issues={issues} grid={grid} readOnly={readOnly} />;
  const change = v1Change(onChange);
  return (
    <div className="grid min-w-0 gap-5">
      <fieldset disabled={readOnly} className="grid min-w-0 gap-5 xl:grid-cols-2">
        <legend className="sr-only">Setup</legend>
        <WordsSection spec={spec} onChange={onChange} issues={issues} />
        <SourceSection spec={spec} onChange={onChange} issues={issues} grid={grid} />
        <KpiSection spec={spec} onChange={change} issues={issues} grid={grid} />
        <LayoutSection spec={spec} onChange={change} issues={issues} />
        <ChannelsSection
          className="xl:col-span-2"
          channels={spec.channels}
          onChannels={(f) => change((s) => ({ ...s, channels: f(s.channels) }))}
          issues={issues}
          grid={grid}
        />
      </fieldset>
    </div>
  );
}

type PartProps = Omit<SpecEditorProps, "grid" | "readOnly">;
interface V1PartProps {
  spec: DashboardSpec;
  onChange: (next: V1Update) => void;
  issues: readonly FormIssue[];
}

/** The words of the dashboard (either layout). */
export function WordsSection({ spec, onChange, issues }: PartProps) {
  return (
    <Section title="Words" description="Plain text only. The client sees these words on the dashboard.">
      <Field label="Title" htmlFor="ds-title" error={issueAt(issues, "/title")}>
        <Input id="ds-title" maxLength={MAX_TITLE} value={spec.title} onChange={(e) => onChange((s) => ({ ...s, title: e.target.value }))} />
      </Field>
      <Field label="Client name (optional)" htmlFor="ds-client" error={issueAt(issues, "/client_name")} hint="Shows above the title and in the client's portal.">
        <Input id="ds-client" maxLength={MAX_CLIENT_NAME} value={spec.client_name ?? ""} onChange={(e) => onChange((s) => ({ ...s, client_name: e.target.value }))} />
      </Field>
      <Field label="Summary (optional)" htmlFor="ds-summary" error={issueAt(issues, "/summary")} hint={`One or two sentences under the title. At most ${MAX_SUMMARY} characters.`}>
        <Textarea id="ds-summary" maxLength={MAX_SUMMARY} value={spec.summary ?? ""} onChange={(e) => onChange((s) => ({ ...s, summary: e.target.value }))} />
      </Field>
      <Field label="Note (optional)" htmlFor="ds-note" error={issueAt(issues, "/note")} hint="For example: “Spend is net media spend. Agency fees are not included.”">
        <Textarea id="ds-note" maxLength={MAX_NOTE} value={spec.note ?? ""} onChange={(e) => onChange((s) => ({ ...s, note: e.target.value }))} />
      </Field>
    </Section>
  );
}

/** The source of the dashboard (either layout). */
export function SourceSection({ spec, onChange, issues, grid }: PartProps & { grid: SourceGrid }) {
  const src = spec.source;
  const endpoints = useEndpoints();
  const models = useModels();
  const versions = useModelVersions(src.kind === "model_version" ? (src.model_id ?? undefined) : undefined);
  const setSource = (next: DashboardSource) =>
    onChange((s) => ({ ...s, source: next, refresh: { ...s.refresh, follow_endpoint: next.kind === "endpoint" ? (s.refresh?.follow_endpoint ?? false) : false } }));
  return (
    <Section title="Source" description="The model that makes the numbers. Publishing pins the version, so a refit does not change a published dashboard by surprise.">
      <fieldset className="grid gap-2">
        <legend className="mb-1 text-sm font-semibold">Use</legend>
        <div className="flex flex-wrap gap-4">
          {(
            [
              ["endpoint", "An endpoint (recommended)"],
              ["model_version", "A model version"],
            ] as const
          ).map(([kind, label]) => (
            <label key={kind} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="ds-source-kind"
                value={kind}
                checked={src.kind === kind}
                onChange={() => setSource(kind === "endpoint" ? { kind, alias: endpoints.data?.items[0]?.alias ?? null } : { kind, model_id: models.data?.items[0]?.id ?? null, version: null })}
                className="accent-[var(--primary)]"
              />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      {src.kind === "endpoint" ? (
        <Field label="Endpoint" htmlFor="ds-alias" error={issueAt(issues, "/source/alias")} hint="An endpoint can follow promotions: see Inputs › Refresh.">
          <NativeSelect id="ds-alias" value={src.alias ?? ""} onChange={(e) => setSource({ kind: "endpoint", alias: e.target.value || null })}>
            <option value="">{endpoints.isPending ? "Loading…" : "Choose an endpoint"}</option>
            {endpoints.data?.items.map((e) => (
              <option key={e.alias} value={e.alias}>
                {e.alias} ({e.model_ref})
              </option>
            ))}
          </NativeSelect>
        </Field>
      ) : (
        <div className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Field label="Model" htmlFor="ds-model" error={issueAt(issues, "/source/model_id") ?? issueAt(issues, "/source")}>
            <NativeSelect id="ds-model" value={src.model_id ?? ""} onChange={(e) => setSource({ kind: "model_version", model_id: e.target.value || null, version: null })}>
              <option value="">{models.isPending ? "Loading…" : "Choose a model"}</option>
              {models.data?.items.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Version" htmlFor="ds-version" error={issueAt(issues, "/source/version")}>
            <NativeSelect id="ds-version" value={src.version ?? ""} disabled={!src.model_id} onChange={(e) => setSource({ ...src, version: e.target.value ? Number(e.target.value) : null })}>
              <option value="">Choose</option>
              {versions.data?.items.map((v) => (
                <option key={v.id} value={v.version}>
                  v{v.version}
                </option>
              ))}
            </NativeSelect>
          </Field>
        </div>
      )}
      {isLayout2(spec) && spec.preset !== "mmm_planner" ? null : <GridStatus grid={grid} />}
    </Section>
  );
}

function GridStatus({ grid }: { grid: SourceGrid }) {
  if (grid.loading) return <p className="text-xs text-muted-foreground">Looking for the model's response grid…</p>;
  if (grid.channels === null)
    return (
      <InlineMessage tone="warning" size="s" role="none">
        This model has no response grid. The dashboard cannot have what-ifs or plan scenarios.
      </InlineMessage>
    );
  if (grid.channels)
    return (
      <InlineMessage tone="success" size="s" role="none">
        The response grid has {grid.channels.length} channels{grid.kpi ? ` and the KPI ${grid.kpi}` : ""}.
      </InlineMessage>
    );
  return null;
}

function KpiSection({ spec, onChange, issues, grid }: V1PartProps & { grid: SourceGrid }) {
  const k = spec.kpi;
  const setKpi = (patch: Partial<KpiDef>) => onChange((s) => ({ ...s, kpi: { ...s.kpi, ...patch } }));
  return (
    <Section title="KPI" description="The result that the dashboard explains, for example revenue or orders.">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Model variable" htmlFor="ds-kpi-var" error={issueAt(issues, "/kpi/variable")} hint={grid.kpi && grid.kpi !== k.variable ? `The grid's KPI is ${grid.kpi}.` : undefined}>
          <Input id="ds-kpi-var" className="font-mono" value={k.variable} onChange={(e) => setKpi({ variable: e.target.value.trim() })} />
        </Field>
        <Field label="Label" htmlFor="ds-kpi-label" error={issueAt(issues, "/kpi/label")}>
          <Input id="ds-kpi-label" maxLength={MAX_LABEL} value={k.label} onChange={(e) => setKpi({ label: e.target.value })} />
        </Field>
        <Field label="Unit" htmlFor="ds-kpi-unit">
          <NativeSelect id="ds-kpi-unit" value={k.unit} onChange={(e) => setKpi({ unit: e.target.value as KpiDef["unit"], currency: e.target.value === "currency" ? (k.currency ?? "EUR") : null })}>
            {UNITS.map((u) => (
              <option key={u} value={u}>
                {UNIT_LABEL[u]}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {k.unit === "currency" ? (
          <Field label="Currency" htmlFor="ds-kpi-cur" error={issueAt(issues, "/kpi/currency")} hint="A code of 3 letters, for example EUR.">
            <Input id="ds-kpi-cur" maxLength={3} className="uppercase" value={k.currency ?? ""} onChange={(e) => setKpi({ currency: e.target.value.toUpperCase() })} />
          </Field>
        ) : null}
        <Field label="Decimals" htmlFor="ds-kpi-dec" error={issueAt(issues, "/kpi/decimals")}>
          <Input id="ds-kpi-dec" type="number" min={0} max={4} step={1} value={k.decimals ?? 0} onChange={(e) => setKpi({ decimals: Number(e.target.value) })} />
        </Field>
        {k.unit === "count" ? (
          <Field label="Value of one unit (optional)" htmlFor="ds-kpi-vpu" error={issueAt(issues, "/kpi/value_per_unit")} hint="In the spend's currency. With it, the dashboard shows where each channel's spend sits.">
            <Input
              id="ds-kpi-vpu"
              type="number"
              min={0}
              step="any"
              value={k.value_per_unit ?? ""}
              onChange={(e) => setKpi({ value_per_unit: e.target.value === "" ? null : Number(e.target.value) })}
            />
          </Field>
        ) : null}
      </div>
    </Section>
  );
}

function LayoutSection({ spec, onChange, issues }: V1PartProps) {
  const tiles = spec.tiles ?? [];
  const tabs = spec.tabs ?? ["this_period"];
  const whatif = spec.whatif?.enabled ?? true;
  const actuals = spec.actuals?.enabled ?? false;
  return (
    <Section title="Layout" description="The planner layout: up to 4 tiles, the channel scorecard and the tabs.">
      <Field label="Readouts are for each" htmlFor="ds-period">
        <NativeSelect id="ds-period" value={spec.period_label ?? "week"} onChange={(e) => onChange((s) => ({ ...s, period_label: e.target.value as NonNullable<DashboardSpec["period_label"]> }))}>
          {PERIODS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </NativeSelect>
      </Field>
      <fieldset className="grid gap-2" aria-describedby="ds-tiles-count">
        <legend className="text-sm font-semibold">Tiles</legend>
        <p id="ds-tiles-count" className="text-xs text-muted-foreground">
          {tiles.length} of {MAX_TILES} chosen.
        </p>
        {TILE_KEYS.map((t) => {
          const on = tiles.includes(t);
          return (
            <label key={t} className={cn("flex items-start gap-2 text-sm", !on && tiles.length >= MAX_TILES && "text-hint")}>
              <Checkbox className="mt-0.5" checked={on} disabled={!on && tiles.length >= MAX_TILES} onCheckedChange={() => onChange((s) => ({ ...s, tiles: toggleTile(s.tiles ?? [], t) }))} />
              <span className="grid gap-0.5">
                <span className="font-medium">{TILE_LABEL[t].label}</span>
                <span className="text-xs text-muted-foreground">{TILE_LABEL[t].hint}</span>
              </span>
            </label>
          );
        })}
        {issueAt(issues, "/tiles") ? (
          <p role="alert" className="text-xs font-medium text-destructive">
            {issueAt(issues, "/tiles")}
          </p>
        ) : null}
      </fieldset>
      <fieldset className="grid gap-2">
        <legend className="text-sm font-semibold">Tabs</legend>
        {TAB_KEYS.map((t) => {
          const needs = t === "scenarios" && !whatif ? "Turn on what-ifs on the Inputs tab first." : t === "update" && !actuals ? "Turn on uploads on the Inputs tab first." : null;
          const on = tabs.includes(t);
          const locked = t === "this_period" || (needs !== null && !on);
          return (
            <label key={t} className={cn("flex items-start gap-2 text-sm", locked && t !== "this_period" && "text-hint")}>
              <Checkbox className="mt-0.5" checked={on} disabled={locked} onCheckedChange={() => onChange((s) => ({ ...s, tabs: toggleTab(s.tabs ?? [], t) }))} />
              <span className="grid gap-0.5">
                <span className="font-medium">{TAB_LABEL[t].label}</span>
                <span className="text-xs text-muted-foreground">{needs ?? TAB_LABEL[t].hint}</span>
              </span>
            </label>
          );
        })}
        {issueAt(issues, "/tabs") ? (
          <p role="alert" className="text-xs font-medium text-destructive">
            {issueAt(issues, "/tabs")}
          </p>
        ) : null}
      </fieldset>
      <div className="flex items-start gap-3">
        <Switch id="ds-technical" checked={spec.show_technical ?? false} onCheckedChange={(on) => onChange((s) => ({ ...s, show_technical: on }))} />
        <span className="grid gap-0.5">
          <Label htmlFor="ds-technical">Let the client open the technical details</Label>
          <span className="text-xs text-muted-foreground">Model version, fit checks and intervals, behind a toggle.</span>
        </span>
      </div>
    </Section>
  );
}

export interface ChannelsSectionProps {
  channels: readonly ChannelDef[];
  onChannels: (f: (c: ChannelDef[]) => ChannelDef[]) => void;
  issues: readonly FormIssue[];
  grid: SourceGrid;
  /** Where the channels are in the spec (`/channels`, or `/mmm/channels` on layout 2). */
  at?: string;
  className?: string;
}

/** The media channels (layout 1, and the planner of layout 2). */
export function ChannelsSection({ channels, onChannels, issues, grid, at = "/channels", className }: ChannelsSectionProps) {
  const setChannel = (i: number, patch: Partial<ChannelDef>) => onChannels((cs) => cs.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const fromGrid = grid.channels ?? null;
  const missing = fromGrid ? fromGrid.filter((n) => !channels.some((c) => c.name === n)) : [];
  const num = (v: string) => (v === "" ? undefined : Number(v));
  return (
    <Section
      className={className}
      title="Channels"
      description="The media channels in the scorecard. The slider range is a multiple of the current plan (1× = the plan)."
      action={
        <div className="flex flex-wrap gap-2">
          {fromGrid ? (
            <Button size="sm" variant="secondary" disabled={!missing.length} onClick={() => onChannels((cs) => channelsFrom([...cs.map((c) => c.name), ...fromGrid], cs))}>
              <Plus /> Add the grid's channels{missing.length ? ` (${missing.length})` : ""}
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            disabled={channels.length >= MAX_CHANNELS}
            onClick={() => onChannels((cs) => [...cs, { name: "", label: "", min_mult: 0.5, max_mult: 1.5, step: 0.05 }])}
          >
            <Plus /> Add a channel
          </Button>
        </div>
      }
    >
      {issueAt(issues, at) ? (
        <p role="alert" className="text-xs font-medium text-destructive">
          {issueAt(issues, at)}
        </p>
      ) : null}
      <div className="min-w-0 overflow-x-auto">
        <Table aria-label="Channels">
          <THead>
            <TR>
              <TH>Model name</TH>
              <TH>Label</TH>
              <TH>Lowest</TH>
              <TH>Highest</TH>
              <TH>Step</TH>
              <TH>
                <span className="sr-only">Actions</span>
              </TH>
            </TR>
          </THead>
          <TBody>
            {channels.map((c, i) => {
              const p = `${at}/${i}`;
              const err = issueAt(issues, `${p}/name`) ?? issueAt(issues, `${p}/label`) ?? issueAt(issues, `${p}/min_mult`) ?? issueAt(issues, `${p}/step`) ?? issueAt(issues, p, true);
              const who = c.label || c.name || `channel ${i + 1}`;
              return (
                <TR key={i} data-channel={c.name}>
                  <TD className="min-w-36 align-top">
                    <Input aria-label={`Model name of ${who}`} className="h-8 font-mono" value={c.name} onChange={(e) => setChannel(i, { name: e.target.value.trim() })} aria-invalid={Boolean(issueAt(issues, `${p}/name`)) || undefined} />
                    {err ? (
                      <p role="alert" className="mt-1 max-w-[40ch] text-xs font-medium text-destructive">
                        {err}
                      </p>
                    ) : null}
                  </TD>
                  <TD className="min-w-40 align-top">
                    <Input aria-label={`Label of ${who}`} className="h-8" maxLength={MAX_LABEL} value={c.label} onChange={(e) => setChannel(i, { label: e.target.value })} />
                  </TD>
                  <TD className="w-24 align-top">
                    <Input aria-label={`Lowest multiple of ${who}`} className="h-8" type="number" min={0} max={1} step={0.05} value={c.min_mult ?? 0.5} onChange={(e) => setChannel(i, { min_mult: num(e.target.value) })} />
                  </TD>
                  <TD className="w-24 align-top">
                    <Input aria-label={`Highest multiple of ${who}`} className="h-8" type="number" min={1} max={5} step={0.05} value={c.max_mult ?? 1.5} onChange={(e) => setChannel(i, { max_mult: num(e.target.value) })} />
                  </TD>
                  <TD className="w-24 align-top">
                    <Input aria-label={`Step of ${who}`} className="h-8" type="number" min={0.01} step={0.01} value={c.step ?? 0.05} onChange={(e) => setChannel(i, { step: num(e.target.value) })} />
                  </TD>
                  <TD className="text-right align-top">
                    <Button variant="ghost" size="icon-sm" aria-label={`Remove ${who}`} onClick={() => onChannels((cs) => cs.filter((_, j) => j !== i))}>
                      <Trash2 />
                    </Button>
                  </TD>
                </TR>
              );
            })}
          </TBody>
        </Table>
      </div>
    </Section>
  );
}
