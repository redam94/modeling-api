/**
 * New dashboard: a title, a source and the KPI. The channels come from the source's response
 * grid (or are typed in for a model without one); the rest of the draft takes the defaults
 * and is edited on the Setup and Inputs tabs.
 */
import { Plus } from "lucide-react";
import { useState, type FormEvent } from "react";

import { useEndpoints } from "@/api/integrations";
import { useModels, useModelVersions } from "@/api/queries";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";

import { useCreateDashboard } from "../api";
import type { Dashboard, DashboardSource, KpiDef } from "../types";
import { useSourceGrid } from "./source";
import { channelLabel, defaultSpec, issueAt, MAX_TITLE, nameFromTitle, NAME_RE, specIssues, UNIT_LABEL } from "./spec-form";

const UNITS = ["currency", "count", "ratio", "percent"] as const;

export function NewDashboardDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (d: Dashboard) => void }) {
  const create = useCreateDashboard();
  const endpoints = useEndpoints();
  const models = useModels();
  const [title, setTitle] = useState("");
  const [name, setName] = useState<string | null>(null);
  const [kind, setKind] = useState<DashboardSource["kind"]>("endpoint");
  const [alias, setAlias] = useState<string | null>(null);
  const [modelId, setModelId] = useState<string | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [kpiVar, setKpiVar] = useState<string | null>(null);
  const [kpiLabel, setKpiLabel] = useState<string | null>(null);
  const [unit, setUnit] = useState<KpiDef["unit"]>("currency");
  const [currency, setCurrency] = useState("EUR");
  const [typed, setTyped] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const effAlias = alias ?? endpoints.data?.items[0]?.alias ?? null;
  const effModel = modelId ?? models.data?.items[0]?.id ?? null;
  const versions = useModelVersions(kind === "model_version" ? (effModel ?? undefined) : undefined);
  const effVersion = version ?? versions.data?.items.reduce((m, v) => Math.max(m, v.version), 0) ?? null;
  const source: DashboardSource = kind === "endpoint" ? { kind, alias: effAlias } : { kind, model_id: effModel, version: effVersion || null };
  const grid = useSourceGrid(source);
  const variable = kpiVar ?? grid.kpi ?? "";
  const label = kpiLabel ?? (variable ? channelLabel(variable) : "");
  const channels = grid.channels ?? typed.split(/[\s,]+/).filter(Boolean);
  const effName = name ?? nameFromTitle(title);
  const spec = defaultSpec({
    title,
    source,
    kpi: { variable, label, unit, currency: unit === "currency" ? currency.trim().toUpperCase() : null, decimals: 0 },
    channels,
    hasGrid: Boolean(grid.channels),
  });
  const issues = specIssues(spec, { gridChannels: grid.channels });
  const nameError = NAME_RE.test(effName) ? undefined : "Use letters, digits, _, . and -. Start with a letter.";
  const err = (p: string, deep = false) => (submitted ? issueAt(issues, p, deep) : undefined);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (issues.length || nameError) return;
    create.mutate({ name: effName, spec }, { onSuccess: onCreated });
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>New client dashboard</DialogTitle>
        <DialogDescription>Choose the model and the KPI. You can change everything later. Nobody outside your team sees the dashboard before you publish it.</DialogDescription>
        <form className="grid gap-5" onSubmit={submit} noValidate>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Title" htmlFor="nd-title" error={err("/title")} hint="The client sees the title.">
              <Input id="nd-title" autoFocus maxLength={MAX_TITLE} placeholder="Weekly media readout" value={title} onChange={(e) => setTitle(e.target.value)} />
            </Field>
            <Field label="Name" htmlFor="nd-name" error={submitted ? nameError : undefined} hint="Only your team sees the name. It must be unique in the project.">
              <Input id="nd-name" className="font-mono" maxLength={128} value={effName} onChange={(e) => setName(e.target.value.trim())} />
            </Field>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Source" htmlFor="nd-kind">
              <NativeSelect id="nd-kind" value={kind} onChange={(e) => setKind(e.target.value as DashboardSource["kind"])}>
                <option value="endpoint">An endpoint (recommended)</option>
                <option value="model_version">A model version</option>
              </NativeSelect>
            </Field>
            {kind === "endpoint" ? (
              <Field label="Endpoint" htmlFor="nd-alias" error={err("/source/alias")}>
                <NativeSelect id="nd-alias" value={effAlias ?? ""} onChange={(e) => setAlias(e.target.value || null)}>
                  {endpoints.data?.items.length ? null : <option value="">{endpoints.isPending ? "Loading…" : "No endpoints"}</option>}
                  {endpoints.data?.items.map((ep) => (
                    <option key={ep.alias} value={ep.alias}>
                      {ep.alias}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
            ) : (
              <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,1fr)] gap-2">
                <Field label="Model" htmlFor="nd-model" error={err("/source/model_id")}>
                  <NativeSelect
                    id="nd-model"
                    value={effModel ?? ""}
                    onChange={(e) => {
                      setModelId(e.target.value || null);
                      setVersion(null);
                    }}
                  >
                    {models.data?.items.length ? null : <option value="">{models.isPending ? "Loading…" : "No models"}</option>}
                    {models.data?.items.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
                <Field label="Version" htmlFor="nd-version" error={err("/source/version")}>
                  <NativeSelect id="nd-version" value={effVersion ?? ""} onChange={(e) => setVersion(Number(e.target.value))}>
                    {versions.data?.items.map((v) => (
                      <option key={v.id} value={v.version}>
                        v{v.version}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
              </div>
            )}
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="KPI variable" htmlFor="nd-kpi" error={err("/kpi/variable")} hint={grid.kpi ? `From the response grid: ${grid.kpi}.` : "The model's output, for example revenue."}>
              <Input id="nd-kpi" className="font-mono" value={variable} onChange={(e) => setKpiVar(e.target.value.trim())} />
            </Field>
            <Field label="KPI label" htmlFor="nd-kpi-label" error={err("/kpi/label")}>
              <Input id="nd-kpi-label" maxLength={60} value={label} onChange={(e) => setKpiLabel(e.target.value)} />
            </Field>
            <Field label="Unit" htmlFor="nd-unit" error={err("/kpi/currency")}>
              <div className="flex gap-2">
                <NativeSelect id="nd-unit" className="min-w-0 flex-1" value={unit} onChange={(e) => setUnit(e.target.value as KpiDef["unit"])}>
                  {UNITS.map((u) => (
                    <option key={u} value={u}>
                      {UNIT_LABEL[u]}
                    </option>
                  ))}
                </NativeSelect>
                {unit === "currency" ? <Input aria-label="Currency" className="w-20 uppercase" maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value)} /> : null}
              </div>
            </Field>
          </div>
          {grid.loading ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner /> Looking for the model's response grid…
            </p>
          ) : grid.channels ? (
            <p className="text-sm text-muted-foreground">
              Channels from the response grid: <span className="text-foreground">{grid.channels.map(channelLabel).join(", ")}</span>. What-ifs are on.
            </p>
          ) : (
            <Field
              label="Channels"
              htmlFor="nd-channels"
              error={err("/channels", true)}
              hint={grid.channels === null ? "This model has no response grid, so what-ifs are off. Type the model names of the channels, separated by commas." : "Type the model names of the channels, separated by commas."}
            >
              <Input id="nd-channels" className="font-mono" placeholder="tv, search, social" value={typed} onChange={(e) => setTyped(e.target.value)} />
            </Field>
          )}
          {create.isError ? <ErrorNotice error={create.error} title="The dashboard was not created" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : <Plus />} Create dashboard
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
