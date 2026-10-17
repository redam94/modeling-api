/**
 * Automation: schedules (a cron editor with presets, a time zone and a preview of the next
 * firing times computed the way the server computes them) and triggers (run when a dataset gets
 * a new version, bound to one of the workflow's inputs, with a debounce).
 */
import { Link } from "@tanstack/react-router";
import { CalendarClock, Database, Plus, Trash2 } from "lucide-react";
import { useId, useMemo, useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { useDatasets } from "@/api/queries";
import { EmptyState, ErrorNotice, Mono, QueryView, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { relativeTime, shortId } from "@/lib/format";
import { toast } from "@/stores/toast";

import { useScheduleMutations, useSchedules, useTriggerMutations, useTriggers } from "./api";
import { CRON_PRESETS, cronProblem, describeCron, isTimeZone, nextFires } from "./cron";
import { DatasetRefPicker } from "./run-inputs";
import type { WorkflowDoc } from "./types";

const FALLBACK_ZONES = ["UTC", "Europe/London", "Europe/Berlin", "Europe/Paris", "America/New_York", "America/Chicago", "America/Los_Angeles", "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney"];

function timeZones(): string[] {
  const f = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
  try {
    const all = f?.("timeZone");
    return all?.length ? ["UTC", ...all.filter((z) => z !== "UTC")] : FALLBACK_ZONES;
  } catch {
    return FALLBACK_ZONES;
  }
}

function localZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function fmtIn(ms: number, tz: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, { timeZone: tz, weekday: "short", year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }).format(new Date(ms));
  } catch {
    return new Date(ms).toISOString();
  }
}

/** The cron field with presets, a plain reading and the next firing times. */
export function CronEditor({ cron, timezone, onCron, onTimezone }: { cron: string; timezone: string; onCron: (v: string) => void; onTimezone: (v: string) => void }) {
  const id = useId();
  const zones = useMemo(timeZones, []);
  const problem = cron.trim() ? cronProblem(cron) : "Write a schedule or pick one above.";
  const tzProblem = isTimeZone(timezone) ? null : "Unknown time zone.";
  const preview = useMemo(() => {
    if (problem || tzProblem) return [];
    try {
      return nextFires(cron, timezone, 5);
    } catch {
      return [];
    }
  }, [cron, timezone, problem, tzProblem]);
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Common schedules">
        {CRON_PRESETS.map((p) => (
          <Button key={p.cron} type="button" size="sm" variant={cron === p.cron ? "secondary" : "outline"} onClick={() => onCron(p.cron)}>
            {p.label}
          </Button>
        ))}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Cron expression" htmlFor={`${id}-cron`} error={cron.trim() ? problem : null} hint="minute hour day month weekday, e.g. 0 6 * * 1; or @daily. At most every 15 minutes.">
          <Input id={`${id}-cron`} className="font-mono" value={cron} onChange={(e) => onCron(e.target.value)} spellCheck={false} />
        </Field>
        <Field label="Time zone" htmlFor={`${id}-tz`} error={tzProblem} hint="Daylight-saving changes are handled: a skipped time does not fire, a repeated one fires once.">
          <Input id={`${id}-tz`} list={`${id}-zones`} value={timezone} onChange={(e) => onTimezone(e.target.value)} spellCheck={false} />
          <datalist id={`${id}-zones`}>
            {zones.map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
        </Field>
      </div>
      {preview.length ? (
        <div className="rounded-md border bg-muted/30 p-3" aria-live="polite" data-testid="cron-preview">
          <p className="text-[13px] font-medium">{describeCron(cron)} ({timezone}). Next runs:</p>
          <ol className="mt-1 grid gap-0.5 text-[13px] tabular">
            {preview.map((t) => (
              <li key={t}>{fmtIn(t, timezone)}</li>
            ))}
          </ol>
        </div>
      ) : null}
    </div>
  );
}

function NewSchedule({ workflowId, doc, onDone }: { workflowId: string; doc: WorkflowDoc; onDone: () => void }) {
  const { create } = useScheduleMutations(workflowId);
  const [cron, setCron] = useState("0 6 * * 1");
  const [timezone, setTimezone] = useState(localZone);
  const [description, setDescription] = useState("");
  const dataInput = Object.entries(doc.inputs ?? {}).find(([, d]) => d.type === "dataset")?.[0];
  const [data, setData] = useState(dataInput ? (doc.inputs?.[dataInput]?.default ?? "") : "");
  const bad = cronProblem(cron) ?? (isTimeZone(timezone) ? null : "Unknown time zone.");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (bad) return;
    create.mutate(
      { cron, timezone, description: description || null, inputs: dataInput && data ? { [dataInput]: data } : {}, params: {}, active: true },
      {
        onSuccess: (s) => {
          toast.success("Schedule added", s.next_run_at ? `Next run ${relativeTime(s.next_run_at)}` : undefined);
          onDone();
        },
      },
    );
  };
  return (
    <form className="grid gap-4 rounded-lg border p-4" onSubmit={submit} aria-label="New schedule">
      <CronEditor cron={cron} timezone={timezone} onCron={setCron} onTimezone={setTimezone} />
      {dataInput ? <DatasetRefPicker value={data} onChange={setData} label="Data for each run" required={!doc.inputs?.[dataInput]?.default} hint="Pick “The latest when it runs” to always use the newest version." /> : null}
      <Field label="Description (optional)" htmlFor="sched-desc">
        <Input id="sched-desc" value={description} maxLength={500} onChange={(e) => setDescription(e.target.value)} placeholder="Weekly refresh" />
      </Field>
      {create.isError ? <ErrorNotice error={create.error} title="The schedule was not added" /> : null}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={Boolean(bad) || create.isPending}>
          {create.isPending ? <Spinner /> : <CalendarClock />} Add schedule
        </Button>
      </div>
    </form>
  );
}

function NewTrigger({ workflowId, doc, onDone }: { workflowId: string; doc: WorkflowDoc; onDone: () => void }) {
  const { create } = useTriggerMutations(workflowId);
  const datasets = useDatasets();
  const dataInputs = Object.entries(doc.inputs ?? {}).filter(([, d]) => d.type === "dataset").map(([k]) => k);
  const [dataset, setDataset] = useState("");
  const [input, setInput] = useState(dataInputs[0] ?? "");
  const [debounce, setDebounce] = useState(300);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!dataset || !input) return;
    create.mutate({ dataset, input, debounce_seconds: debounce, event: "dataset.version.created", params: {}, active: true }, {
        onSuccess: () => {
          toast.success("Trigger added");
          onDone();
        },
      });
  };
  if (!dataInputs.length) return <p className="text-[13px] text-muted-foreground">This workflow has no dataset input, so a new dataset version cannot start it.</p>;
  return (
    <form className="grid gap-4 rounded-lg border p-4" onSubmit={submit} aria-label="New trigger">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="When this dataset gets a new version" htmlFor="trg-ds">
          <NativeSelect id="trg-ds" value={dataset} onChange={(e) => setDataset(e.target.value)}>
            <option value="">Choose a dataset</option>
            {(datasets.data?.items ?? []).map((d) => (
              <option key={d.id} value={d.ref}>
                {d.name}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Field label="Run with it as" htmlFor="trg-input">
          <NativeSelect id="trg-input" value={input} onChange={(e) => setInput(e.target.value)}>
            {dataInputs.map((k) => (
              <option key={k} value={k}>
                inputs.{k}
              </option>
            ))}
          </NativeSelect>
        </Field>
      </div>
      <Field label="Wait for more uploads (seconds)" htmlFor="trg-debounce" hint="Versions that arrive within this time start one run, on the newest.">
        <Input id="trg-debounce" type="number" min={0} max={86400} value={debounce} onChange={(e) => setDebounce(Math.max(0, Number(e.target.value) || 0))} />
      </Field>
      {create.isError ? <ErrorNotice error={create.error} title="The trigger was not added" /> : null}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={!dataset || !input || create.isPending}>
          {create.isPending ? <Spinner /> : <Database />} Add trigger
        </Button>
      </div>
    </form>
  );
}

export function AutomationPanel({ workflowId, doc }: { workflowId: string; doc: WorkflowDoc }) {
  const ws = useWorkspace();
  const mayEdit = can(ws.grants, "update", "workflow");
  const schedules = useSchedules(workflowId);
  const triggers = useTriggers(workflowId);
  const sched = useScheduleMutations(workflowId);
  const trig = useTriggerMutations(workflowId);
  const [adding, setAdding] = useState<"schedule" | "trigger" | null>(null);
  return (
    <div className="grid gap-5">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Schedules</CardTitle>
            <CardDescription>Run on a timetable, in a time zone. Runs count against the project's quotas like any other.</CardDescription>
          </div>
          {mayEdit && adding !== "schedule" ? (
            <Button size="sm" variant="outline" onClick={() => setAdding("schedule")}>
              <Plus /> Schedule
            </Button>
          ) : null}
        </CardHeader>
        <CardContent className="grid gap-3">
          {adding === "schedule" ? <NewSchedule workflowId={workflowId} doc={doc} onDone={() => setAdding(null)} /> : null}
          <QueryView query={schedules}>
            {(page) =>
              page.items.length ? (
                <ul className="grid gap-2">
                  {page.items.map((s) => (
                    <li key={s.id} className="flex flex-wrap items-center gap-3 rounded-md border p-3" data-schedule={s.id}>
                      <div className="min-w-0 flex-1">
                        <p className="text-[13px] font-medium">
                          {describeCron(s.cron)} <span className="text-muted-foreground">({s.timezone})</span>
                        </p>
                        <p className="text-xs text-muted-foreground">
                          <Mono>{s.cron}</Mono>
                          {s.description ? ` · ${s.description}` : ""}
                          {s.active && s.next_run_at ? ` · next ${relativeTime(s.next_run_at)}` : ""}
                          {s.last_run_id ? (
                            <>
                              {" · last "}
                              <Link to="/workflow-runs/$runId" params={{ runId: s.last_run_id }} className="text-primary hover:underline">
                                {shortId(s.last_run_id)}
                              </Link>
                            </>
                          ) : null}
                        </p>
                      </div>
                      <label className="flex items-center gap-2 text-xs">
                        <Switch checked={s.active} disabled={!mayEdit} onCheckedChange={(active) => sched.update.mutate({ id: s.id, body: { active } })} aria-label={`Schedule ${s.cron} active`} />
                        {s.active ? "Active" : "Paused"}
                      </label>
                      {mayEdit ? (
                        <Button variant="ghost" size="icon-sm" aria-label={`Delete schedule ${s.cron}`} onClick={() => sched.remove.mutate(s.id)}>
                          <Trash2 />
                        </Button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : adding ? null : (
                <EmptyState icon={<CalendarClock />} title="No schedule">
                  Add one to run this workflow every week, day or month.
                </EmptyState>
              )
            }
          </QueryView>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div>
            <CardTitle>Triggers</CardTitle>
            <CardDescription>Run whenever a dataset gets a new version. Datasets written by workflows never trigger one, so a workflow cannot trigger itself.</CardDescription>
          </div>
          {mayEdit && adding !== "trigger" ? (
            <Button size="sm" variant="outline" onClick={() => setAdding("trigger")}>
              <Plus /> Trigger
            </Button>
          ) : null}
        </CardHeader>
        <CardContent className="grid gap-3">
          {adding === "trigger" ? <NewTrigger workflowId={workflowId} doc={doc} onDone={() => setAdding(null)} /> : null}
          <QueryView query={triggers}>
            {(page) =>
              page.items.length ? (
                <ul className="grid gap-2">
                  {page.items.map((t) => (
                    <li key={t.id} className="flex flex-wrap items-center gap-3 rounded-md border p-3">
                      <div className="min-w-0 flex-1">
                        <p className="text-[13px] font-medium">
                          New version of <Mono>{t.dataset}</Mono> → inputs.{t.input}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          waits {t.debounce_seconds ?? 300} s for more uploads{t.last_fired_at ? ` · last fired ${relativeTime(t.last_fired_at)}` : ""}
                        </p>
                      </div>
                      <Badge variant={t.active ? "success" : "outline"}>{t.active ? "Active" : "Paused"}</Badge>
                      {mayEdit ? (
                        <Button variant="ghost" size="icon-sm" aria-label={`Delete trigger on ${t.dataset}`} onClick={() => trig.remove.mutate(t.id)}>
                          <Trash2 />
                        </Button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : adding ? null : (
                <EmptyState icon={<Database />} title="No trigger">
                  Add one to run this workflow on every new upload.
                </EmptyState>
              )
            }
          </QueryView>
        </CardContent>
      </Card>
    </div>
  );
}
