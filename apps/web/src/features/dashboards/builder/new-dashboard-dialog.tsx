/**
 * New dashboard (v0.17: always layout 2): a title, a source, and a layout. The app asks which
 * layouts fit the source's model (`GET /v1/dashboards:presets`, best first, with plain reasons
 * and the inputs each cannot offer) and starts from the compiled draft of the suggested one;
 * another layout is applied to that draft in the browser (`switchPreset`). Nothing asks for
 * channels: the media planner takes them from the model's response grid. Everything else is
 * edited on the Setup and Inputs tabs.
 */
import { Plus } from "lucide-react";
import { useState, type FormEvent } from "react";

import { useEndpoints } from "@/api/integrations";
import { useModels, useModelVersions } from "@/api/queries";
import { ErrorNotice, Spinner } from "@/components/common";
import { InlineMessage, TagWord } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { useCreateDashboard, useDashboardPresets } from "../api";
import type { Dashboard, DashboardSource, DashboardSpecV2, PresetKey } from "../types";
import { presetRefOf } from "./setup-v2";
import { useSourceGrid } from "./source";
import { issueAt, MAX_TITLE, nameFromTitle, NAME_RE, specIssues } from "./spec-form";
import { PRESET_WORDS, presetSwitchReason, switchPreset } from "./spec-form-v2";
import { PlainIssues } from "./upgrade-banner";

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
  const [picked, setPicked] = useState<{ ref: string; preset: PresetKey } | null>(null);
  const [submitted, setSubmitted] = useState(false);

  const effAlias = alias ?? endpoints.data?.items[0]?.alias ?? null;
  const effModel = modelId ?? models.data?.items[0]?.id ?? null;
  const versions = useModelVersions(kind === "model_version" ? (effModel ?? undefined) : undefined);
  const effVersion = version ?? versions.data?.items.reduce((m, v) => Math.max(m, v.version), 0) ?? null;
  const source: DashboardSource = kind === "endpoint" ? { kind, alias: effAlias } : { kind, model_id: effModel, version: effVersion || null };
  const ref = presetRefOf(source);
  const refKey = JSON.stringify(ref);
  const presets = useDashboardPresets(ref);
  const grid = useSourceGrid(source);
  const suggestions = presets.data?.suggestions ?? [];
  const base = presets.data?.spec ?? null;
  const chosen: PresetKey | null = (picked?.ref === refKey ? picked.preset : null) ?? base?.preset ?? null;
  const ctx = { gridChannels: grid.channels };
  const spec: DashboardSpecV2 | null = base && chosen ? { ...(chosen === base.preset ? base : switchPreset(base, chosen, ctx).spec), title: title.trim(), source } : null;
  const issues = spec ? specIssues(spec) : [];
  const effName = name ?? nameFromTitle(title);
  const nameError = NAME_RE.test(effName) ? undefined : "Use letters, digits, _, . and -. Start with a letter.";
  const err = (p: string, deep = false) => (submitted ? issueAt(issues, p, deep) : undefined);
  const titleError = submitted && !title.trim() ? "Type a title." : err("/title");
  const loading = ref !== null && presets.isPending;
  const noSpec = presets.isSuccess && !base;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (!spec || issues.length || nameError || !title.trim()) return;
    create.mutate({ name: effName, spec }, { onSuccess: onCreated });
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>New client dashboard</DialogTitle>
        <DialogDescription>Choose the model and a layout. You can change everything later. Nobody outside your team sees the dashboard before you publish it.</DialogDescription>
        <form className="grid gap-5" onSubmit={submit} noValidate>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Title" htmlFor="nd-title" error={titleError} hint="The client sees the title.">
              <Input id="nd-title" autoFocus maxLength={MAX_TITLE} placeholder="Monthly sales forecast" value={title} onChange={(e) => setTitle(e.target.value)} />
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

          <fieldset className="grid min-w-0 gap-2">
            <legend className="mb-1 text-sm font-semibold">Layout</legend>
            {ref === null ? (
              <p className="text-sm text-muted-foreground">Choose a source first.</p>
            ) : loading ? (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner /> Looking at what the model can show…
              </p>
            ) : presets.isError ? (
              <ErrorNotice error={presets.error} title="The app could not look at the model" onRetry={() => void presets.refetch()} />
            ) : (
              <div role="radiogroup" aria-label="Layout" className="grid gap-2 sm:grid-cols-2">
                {suggestions.map((sg, i) => {
                  const why = base ? presetSwitchReason(base, sg.preset, ctx) : "The app could not make a starting draft for this model.";
                  const on = chosen === sg.preset;
                  const id = `nd-preset-${sg.preset}`;
                  return (
                    <label
                      key={sg.preset}
                      htmlFor={id}
                      className={cn("grid cursor-pointer gap-1 rounded-lg border p-3 text-sm", on && "border-primary ring-1 ring-primary", why && "cursor-not-allowed opacity-70")}
                      data-preset={sg.preset}
                    >
                      <span className="flex flex-wrap items-center gap-2">
                        <input
                          id={id}
                          type="radio"
                          name="nd-preset"
                          value={sg.preset}
                          checked={on}
                          disabled={Boolean(why)}
                          onChange={() => setPicked({ ref: refKey, preset: sg.preset })}
                          aria-describedby={`${id}-why`}
                          className="accent-[var(--primary)]"
                        />
                        <span className="font-medium">{PRESET_WORDS[sg.preset].label}</span>
                        {i === 0 ? <TagWord tone="positive">Suggested</TagWord> : null}
                      </span>
                      <span id={`${id}-why`} className="grid gap-0.5 text-xs text-muted-foreground">
                        <span>{why ?? ((sg.reasons ?? []).join(" ") || PRESET_WORDS[sg.preset].hint)}</span>
                        {sg.blocked_inputs?.length ? <span>Not possible with this model: {sg.blocked_inputs.join("; ")}.</span> : null}
                      </span>
                    </label>
                  );
                })}
              </div>
            )}
            {noSpec ? (
              <InlineMessage tone="warning" size="s" role="none">
                The app could not make a starting draft for this model. Choose another source, or ask an admin.
                <PlainIssues issues={presets.data?.issues ?? []} label="Why there is no starting draft" />
              </InlineMessage>
            ) : null}
            {submitted && spec && issues.length ? (
              <InlineMessage tone="error" size="s" role="alert">
                The starting draft has problems:
                <PlainIssues issues={issues} label="Problems of the starting draft" />
              </InlineMessage>
            ) : null}
          </fieldset>
          {create.isError ? <ErrorNotice error={create.error} title="The dashboard was not created" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !spec} title={spec ? undefined : "Choose a source that the app can make a dashboard for."}>
              {create.isPending ? <Spinner /> : <Plus />} Create dashboard
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
