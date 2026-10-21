/**
 * Try a case (v0.17 scenario rows, plan §3.2): the viewer sets the inputs the analyst exposed,
 * within the analyst's ranges, and the model scores the cases. Native form controls only
 * (number, select, date, checkbox): no values grid and no JSON-schema form, so the share app
 * stays small.
 *
 * The browser checks each value as the server does (`dashboard_inputs.check_value`), so a value
 * out of range is marked at the field before anything is sent. The server checks everything
 * again; a refused value comes back with a `/rows/<i>/<name>` pointer and is shown at its
 * field. The result is private to the viewer; a signed-in viewer can keep it for the analyst
 * to review.
 */
import { Plus, RotateCcw, Trash2 } from "lucide-react";
import { useId, useMemo, useState } from "react";
import type * as React from "react";

import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Input, Label, NativeSelect } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { dashboardErrorMessage, problemOf, useKeepReadout, usePredict, useReadout } from "../client";
import type { ClientDashboard, DashboardClientV2, Readout, ScenarioForm, ScenarioInputDef, ScenarioRow } from "../types";
import { checkRows, defaultRow, fieldValue, g, horizonKey, issuesByPointer } from "./panel-model";
import { PredictionView } from "./panels/results";
import { ReadoutState, Section, Working } from "./parts";

function rangeHint(def: ScenarioInputDef): string | null {
  if (def.type === "number" || def.type === "integer") {
    if (def.min === null || def.min === undefined || def.max === null || def.max === undefined) return null;
    return `From ${g(def.min)} to ${g(def.max)}${def.step ? `, in steps of ${g(def.step)}` : ""}.`;
  }
  if (def.type === "date" && def.date_min && def.date_max) return `From ${def.date_min} to ${def.date_max}.`;
  return null;
}

function text(v: string | number | boolean | null | undefined): string {
  return v === null || v === undefined ? "" : String(v);
}

function InputField({
  def,
  value,
  error,
  disabled,
  onChange,
  idBase,
}: {
  def: ScenarioInputDef;
  value: ScenarioRow[string] | undefined;
  error: string | undefined;
  disabled: boolean;
  onChange: (v: ScenarioRow[string]) => void;
  idBase: string;
}) {
  const id = `${idBase}-${def.name}`;
  const hint = [def.help, rangeHint(def)].filter(Boolean).join(" ");
  const describedBy = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(" ") || undefined;
  const common = { id, disabled, "aria-invalid": error ? true : undefined, "aria-describedby": describedBy } as const;
  let control: React.ReactNode;
  if (def.type === "toggle") {
    control = (
      <div className="flex items-center gap-2">
        <input type="checkbox" {...common} checked={value === true} onChange={(e) => onChange(e.target.checked)} className="size-4 accent-primary" />
        <Label htmlFor={id}>{def.label}</Label>
      </div>
    );
  } else if (def.type === "choice") {
    control = (
      <NativeSelect {...common} value={text(value)} onChange={(e) => onChange(e.target.value)}>
        {(def.choices ?? []).map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </NativeSelect>
    );
  } else if (def.type === "date") {
    control = <Input type="date" {...common} value={text(value)} min={def.date_min ?? undefined} max={def.date_max ?? undefined} onChange={(e) => onChange(e.target.value || null)} />;
  } else {
    control = (
      <Input
        type="number"
        inputMode="decimal"
        {...common}
        value={text(value)}
        min={def.min ?? undefined}
        max={def.max ?? undefined}
        step={def.step ?? (def.type === "integer" ? 1 : "any")}
        onChange={(e) => onChange(fieldValue(e.target.value, def))}
        className="tabular"
      />
    );
  }
  return (
    <div className="grid content-start gap-1" data-input={def.name}>
      {def.type === "toggle" ? null : <Label htmlFor={id}>{def.label}</Label>}
      {control}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-hint">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-destructive">
          {error}.
        </p>
      ) : null}
    </div>
  );
}

function Result({ client, dashboard, seq, labels }: { client: DashboardClientV2; dashboard: ClientDashboard; seq: number; labels: Record<string, string> }) {
  const q = useReadout(client, seq);
  const keep = useKeepReadout(client);
  const r: Readout | undefined = q.data;
  if (q.isError) return <InlineMessage tone="error">{dashboardErrorMessage(q.error)}</InlineMessage>;
  if (!r) return <Working>Calculating…</Working>;
  const pending = r.visibility === "pending_review";
  return (
    <div className="grid gap-3" data-readout="tried">
      <ReadoutState readout={r} />
      {r.status === "ready" && r.snapshot?.prediction ? <PredictionView dashboard={dashboard} result={r.snapshot.prediction} labels={labels} /> : null}
      {r.status === "ready" && r.snapshot && !r.snapshot.prediction ? <p className="text-sm text-foreground">{r.snapshot.lead}</p> : null}
      {client.keep && r.status === "ready" ? (
        <div className="grid gap-1">
          <div>
            <Button variant="secondary" size="sm" disabled={pending || keep.isPending} onClick={() => keep.mutate(r.seq)}>
              Keep this case
            </Button>
          </div>
          <p className="text-xs text-hint">{pending ? "Kept. The analyst checks it before other people see it." : "Only you can see this result. Keep it to show it to the analyst."}</p>
          {keep.isError ? <InlineMessage tone="error">{dashboardErrorMessage(keep.error)}</InlineMessage> : null}
        </div>
      ) : null}
    </div>
  );
}

export function TryACase({ client, dashboard, preview = false }: { client: DashboardClientV2; dashboard: ClientDashboard; preview?: boolean }) {
  const form = dashboard.scenario_form;
  if (!form || !(form.inputs?.length || form.horizon)) return <InlineMessage tone="info">This dashboard has no inputs to try.</InlineMessage>;
  return <TryForm client={client} dashboard={dashboard} form={form} preview={preview} />;
}

function TryForm({ client, dashboard, form, preview }: { client: DashboardClientV2; dashboard: ClientDashboard; form: ScenarioForm; preview: boolean }) {
  const idBase = useId();
  const [rows, setRows] = useState<ScenarioRow[]>(() => [defaultRow(form)]);
  const [seq, setSeq] = useState<number | null>(null);
  const [idemKey, setIdemKey] = useState(() => crypto.randomUUID());
  const predict = usePredict(client);
  const paused = dashboard.live_inputs === "paused";
  const local = useMemo(() => checkRows(rows, form), [rows, form]);
  const server = issuesByPointer(predict.isError ? (problemOf(predict.error)?.problem?.errors ?? undefined) : undefined);
  const hkey = horizonKey(form);
  const labels = Object.fromEntries((form.inputs ?? []).map((i) => [i.name, i.label]));
  if (hkey) labels[hkey] = "Periods ahead";

  const changed = (next: ScenarioRow[]) => {
    setRows(next);
    setSeq(null);
    predict.reset();
    setIdemKey(crypto.randomUUID());
  };
  const set = (i: number, name: string, v: ScenarioRow[string]) => changed(rows.map((r, k) => (k === i ? { ...r, [name]: v } : r)));
  const send = () => predict.mutate({ body: { rows }, idemKey }, { onSuccess: (r) => setSeq(r.seq) });
  const rowsError = local.get("/rows") ?? server.get("/rows");
  const otherServer = predict.isError && server.size === 0;

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-12" data-tab="try">
      <Section
        className="lg:col-span-7"
        title={rows.length > 1 ? "Your cases" : "Your case"}
        description={`Set the values, then calculate. Up to ${form.max_rows} ${form.max_rows === 1 ? "case" : "cases"}. Values outside the ranges the analyst set are not accepted.`}
        action={
          <Button variant="ghost" size="sm" disabled={preview} onClick={() => changed([defaultRow(form)])}>
            <RotateCcw /> Reset
          </Button>
        }
      >
        {paused ? <InlineMessage tone="warning">Trying cases is paused on this dashboard. Your analyst is checking it.</InlineMessage> : null}
        {rows.map((row, i) => (
          <fieldset key={i} className={cn("grid gap-4 rounded-lg border p-4", rows.length === 1 && "border-0 p-0")} data-row={i}>
            {rows.length > 1 ? (
              <legend className="flex w-full items-center justify-between gap-2 px-1 text-sm font-semibold text-foreground">
                Case {i + 1}
                <Button variant="ghost" size="icon-sm" aria-label={`Remove case ${i + 1}`} disabled={preview} onClick={() => changed(rows.filter((_, k) => k !== i))}>
                  <Trash2 />
                </Button>
              </legend>
            ) : (
              <legend className="sr-only">Case 1</legend>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              {(form.inputs ?? []).map((def) => (
                <InputField
                  key={def.name}
                  def={def}
                  idBase={`${idBase}-${i}`}
                  value={row[def.name]}
                  error={local.get(`/rows/${i}/${def.name}`) ?? server.get(`/rows/${i}/${def.name}`)}
                  disabled={preview}
                  onChange={(v) => set(i, def.name, v)}
                />
              ))}
              {hkey && form.horizon ? (
                <InputField
                  def={{ name: hkey, label: "Periods ahead", type: "integer", min: form.horizon.min ?? 1, max: form.horizon.max, step: 1, default: form.horizon.default, bind: { dim: form.horizon.dim } }}
                  idBase={`${idBase}-${i}`}
                  value={row[hkey]}
                  error={local.get(`/rows/${i}/${hkey}`) ?? server.get(`/rows/${i}/${hkey}`)}
                  disabled={preview}
                  onChange={(v) => set(i, hkey, v)}
                />
              ) : null}
            </div>
          </fieldset>
        ))}
        {rowsError ? <InlineMessage tone="error">{rowsError}.</InlineMessage> : null}
        <div className="flex flex-wrap gap-2">
          <Button onClick={send} disabled={preview || paused || local.size > 0 || predict.isPending || seq !== null}>
            Calculate
          </Button>
          <Button variant="action" disabled={preview || rows.length >= form.max_rows} onClick={() => changed([...rows, defaultRow(form)])}>
            <Plus /> Add a case
          </Button>
        </div>
        {preview ? <p className="text-xs text-hint">Inputs are off in the preview.</p> : null}
        {local.size > 0 ? <p className="text-xs text-hint">Correct the values marked in red to calculate.</p> : null}
      </Section>
      <Section className="lg:col-span-5" title="The result" description="Only you can see these results.">
        {predict.isPending ? <Working>Sending your case…</Working> : null}
        {otherServer ? <InlineMessage tone="error">{dashboardErrorMessage(predict.error)}</InlineMessage> : null}
        {predict.isError && server.size > 0 ? <InlineMessage tone="error">The model did not accept some values. They are marked at their fields.</InlineMessage> : null}
        {seq !== null ? <Result client={client} dashboard={dashboard} seq={seq} labels={labels} /> : !predict.isPending ? <p className="text-sm text-muted-foreground">Set the values and select Calculate.</p> : null}
      </Section>
    </div>
  );
}
