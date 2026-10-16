/**
 * Step 2, "Your data": pick a file, answer the template's questions about its columns (with the
 * suggestions role detection makes from the browser's profile of the file), check the live
 * preview, then upload. Uploading creates the dataset, puts the file to the signed URL, starts the
 * sandboxed ingest with the draft's mapping and saves the draft as the session's spec.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { AlertTriangle, CheckCircle2, FileSpreadsheet, FileUp, Info, Sparkles, UploadCloud } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type DragEvent } from "react";

import { can } from "@/api/access";
import { api, ApiError, errorMessage, newIdempotencyKey, unwrap } from "@/api/client";
import { createUpload, qk, useDatasets, useProjectScope, useSpecs } from "@/api/queries";
import type { DatasetVersion } from "@/api/types";
import { putToSignedUrl, type UploadProgress } from "@/api/upload";
import { Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, SegmentedControl } from "@/components/ui/overlays";
import { Badge, Card, CardContent, Field, Input, Label, NativeSelect, Progress, Textarea } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { useDebounced } from "@/hooks/use-debounced";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

import { useDetectRoles, useGuidedDraft } from "../api";
import { StepActions } from "../frame";
import { detectMeridian, detectMff, formatFromName, profilesFromNames, profileTable, readLocalTable, seriesFor, tableProblems, type LocalTable, type PresetMatch, type UploadFormat } from "../profile";
import { clearLocalDraft, defaultSessionName, GUIDED_LABEL, GUIDED_MESSAGE, hasTask, loadLocalDraft, saveLocalDraft, slugify } from "../session";
import type { ColumnProfile, GuidedAnswers, GuidedDraft, GuidedQuestion, GuidedTemplate, QuestionSuggestion } from "../types";
import { Sparkline } from "../ui";

type Answers = Record<string, unknown>;

const PREVIEW_ROWS = 8;

function confidenceWord(c: number): string {
  return c >= 0.8 ? "Very likely" : c >= 0.5 ? "Likely" : "Possible";
}

function isAnswered(q: GuidedQuestion, v: unknown): boolean {
  if (q.kind === "columns") return Array.isArray(v) && v.length > 0;
  if (q.kind === "number") return typeof v === "number" && Number.isFinite(v);
  return typeof v === "string" && v !== "";
}

/** The answers as the draft endpoint takes them (unanswered optional questions left out). */
export function normalizeAnswers(template: GuidedTemplate, answers: Answers): Record<string, string | number | string[]> {
  const out: Record<string, string | number | string[]> = {};
  for (const q of template.questions) {
    const v = answers[q.id];
    if (!isAnswered(q, v)) continue;
    out[q.id] = q.kind === "columns" ? (v as string[]) : (v as string | number);
  }
  return out;
}

export function missingRequired(template: GuidedTemplate, answers: Answers): GuidedQuestion[] {
  return template.questions.filter((q) => q.required !== false && q.default == null && !isAnswered(q, answers[q.id]));
}

const ROLE_WORD: Record<string, string> = { kpi: "Result", date: "Date", geo: "Market", spend: "Spend", controls: "Other driver", store: "Store", drivers: "Driver", customer: "Customer", occasion: "Trip", choice: "Choice", prices: "Price", promos: "Promotion" };

function roleOfColumn(template: GuidedTemplate, answers: Answers, column: string): string | null {
  for (const q of template.questions) {
    const v = answers[q.id];
    if (v === column || (Array.isArray(v) && v.includes(column))) return ROLE_WORD[q.id] ?? q.id;
  }
  return null;
}

function compatible(q: GuidedQuestion, p: ColumnProfile): boolean {
  return !q.accepts?.length || q.accepts.includes(p.dtype);
}

// ----------------------------------------------------------------------------- questions

function SuggestionNote({ suggestion, column }: { suggestion: QuestionSuggestion | undefined; column: string }) {
  const c = suggestion?.candidates?.find((x) => x.column === column);
  if (!c) return null;
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      <Sparkles aria-hidden className="size-3.5 text-primary" />
      <span className="font-medium text-foreground">{confidenceWord(c.confidence)} match</span>
      <span className="tabular">({Math.round(c.confidence * 100)}%)</span>
      <span>· {c.reason_plain}</span>
    </p>
  );
}

function ColumnQuestion({
  q,
  value,
  onChange,
  profiles,
  suggestion,
  error,
}: {
  q: GuidedQuestion;
  value: unknown;
  onChange: (v: unknown) => void;
  profiles: ColumnProfile[];
  suggestion: QuestionSuggestion | undefined;
  error: string | null;
}) {
  const id = useId();
  const ranked = new Map((suggestion?.candidates ?? []).map((c) => [c.column, c.confidence]));
  const options = [...profiles].sort((a, b) => (ranked.get(b.name) ?? -1) - (ranked.get(a.name) ?? -1));
  return (
    <div className="grid gap-1.5" data-question={q.id}>
      <Field label={q.prompt_plain} htmlFor={id} hint={q.help_plain ?? undefined} error={error ?? undefined}>
        <NativeSelect id={id} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose a column…</option>
          {options.map((p) => (
            <option key={p.name} value={p.name} disabled={!compatible(q, p)}>
              {p.name}
              {ranked.has(p.name) && (ranked.get(p.name) ?? 0) >= 0.5 ? " (suggested)" : ""}
            </option>
          ))}
        </NativeSelect>
      </Field>
      {typeof value === "string" && value ? <SuggestionNote suggestion={suggestion} column={value} /> : null}
    </div>
  );
}

function ColumnsQuestion({
  q,
  value,
  onChange,
  profiles,
  suggestion,
  error,
}: {
  q: GuidedQuestion;
  value: unknown;
  onChange: (v: unknown) => void;
  profiles: ColumnProfile[];
  suggestion: QuestionSuggestion | undefined;
  error: string | null;
  taken: Set<string>;
}) {
  const id = useId();
  const [all, setAll] = useState(false);
  const selected = new Set(Array.isArray(value) ? (value as string[]) : []);
  const cands = new Map((suggestion?.candidates ?? []).map((c) => [c.column, c]));
  const fits = profiles.filter((p) => compatible(q, p));
  const shown = all ? fits : fits.filter((p) => cands.has(p.name) || selected.has(p.name));
  const list = shown.length ? shown : fits;
  const toggle = (name: string, on: boolean) => {
    const next = fits.map((p) => p.name).filter((n) => (n === name ? on : selected.has(n)));
    onChange(next);
  };
  return (
    <fieldset className="grid gap-2" data-question={q.id} aria-describedby={error ? `${id}-error` : undefined}>
      <legend className="text-[13px] leading-none font-medium">
        {q.prompt_plain}
        {q.required === false ? <span className="font-normal text-muted-foreground"> (optional)</span> : null}
      </legend>
      {q.help_plain ? <p className="text-xs text-muted-foreground">{q.help_plain}</p> : null}
      <ul className="grid gap-1.5">
        {list.map((p) => {
          const c = cands.get(p.name);
          const cid = `${id}-${p.name}`;
          return (
            <li key={p.name} className={cn("flex items-start gap-3 rounded-md border px-3 py-2", selected.has(p.name) && "border-primary/40 bg-accent/40")}>
              <Checkbox id={cid} checked={selected.has(p.name)} onCheckedChange={(v) => toggle(p.name, v === true)} className="mt-0.5" />
              <Label htmlFor={cid} className="grid min-w-0 flex-1 cursor-pointer gap-1 leading-snug">
                <span className="flex flex-wrap items-center gap-2">
                  <Mono className="break-all">{p.name}</Mono>
                  {c && c.confidence >= 0.5 ? (
                    <Badge variant="info">
                      <Sparkles aria-hidden /> {confidenceWord(c.confidence)}
                    </Badge>
                  ) : null}
                </span>
                {c ? <span className="text-xs font-normal text-muted-foreground">{c.reason_plain}</span> : null}
              </Label>
            </li>
          );
        })}
      </ul>
      {fits.length > list.length || all ? (
        <Button variant="link" size="sm" className="h-auto justify-self-start px-0" onClick={() => setAll((v) => !v)}>
          {all ? "Show suggestions only" : `Show all ${fits.length} number columns`}
        </Button>
      ) : null}
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-xs font-medium text-destructive">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

function OtherQuestion({ q, value, onChange, error }: { q: GuidedQuestion; value: unknown; onChange: (v: unknown) => void; error: string | null }) {
  const id = useId();
  if (q.kind === "choice") {
    const choices = q.choices ?? [];
    return (
      <div className="grid gap-1.5" data-question={q.id}>
        <span className="text-[13px] leading-none font-medium" id={id}>
          {q.prompt_plain}
        </span>
        <SegmentedControl<string>
          label={q.prompt_plain}
          value={typeof value === "string" ? value : typeof q.default === "string" ? q.default : (choices[0]?.value ?? "")}
          onValueChange={onChange}
          options={choices.map((c) => ({ value: c.value, label: c.label }))}
          className="justify-self-start"
        />
        {q.help_plain ? <p className="text-xs text-muted-foreground">{q.help_plain}</p> : null}
      </div>
    );
  }
  return (
    <Field label={q.prompt_plain} htmlFor={id} hint={q.help_plain ?? undefined} error={error ?? undefined}>
      <Input
        id={id}
        type={q.kind === "number" ? "number" : "text"}
        inputMode={q.kind === "number" ? "numeric" : undefined}
        min={q.minimum ?? undefined}
        max={q.maximum ?? undefined}
        value={typeof value === "number" || typeof value === "string" ? String(value) : ""}
        onChange={(e) => onChange(q.kind === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value)}
        className="max-w-40"
      />
    </Field>
  );
}

// ----------------------------------------------------------------------------- preview

function DataPreview({ table, template, answers }: { table: LocalTable; template: GuidedTemplate; answers: Answers }) {
  const dateCol = typeof answers.date === "string" ? answers.date : null;
  const series: { label: string; column: string }[] = [];
  for (const q of template.questions) {
    if (q.dim || (q.kind !== "column" && q.kind !== "columns")) continue;
    const v = answers[q.id];
    const cols = typeof v === "string" && v ? [v] : Array.isArray(v) ? (v as string[]) : [];
    for (const c of cols) series.push({ label: `${ROLE_WORD[q.id] ?? q.id}: ${c}`, column: c });
  }
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <h3 className="text-[13px] font-semibold">Your file</h3>
        <div className="max-h-72 overflow-auto rounded-lg border [contain:inline-size]">
          <table className="w-full text-xs">
            <caption className="sr-only">The first rows of your file, read in your browser</caption>
            <thead className="sticky top-0 bg-card">
              <tr className="border-b">
                {table.header.map((h, i) => {
                  const role = roleOfColumn(template, answers, h);
                  return (
                    <th key={i} scope="col" className="px-2 py-1.5 text-left align-bottom font-medium whitespace-nowrap text-muted-foreground">
                      {role ? <span className="mb-0.5 block text-[10px] font-semibold tracking-wide text-primary uppercase">{role}</span> : null}
                      {h}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {table.rows.slice(0, PREVIEW_ROWS).map((r, i) => (
                <tr key={i} className="border-b last:border-0">
                  {table.header.map((_, j) => (
                    <td key={j} className="px-2 py-1 font-mono whitespace-nowrap tabular">
                      {r[j] ?? ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-muted-foreground">
          {table.mff ? `An MFF long table: ${table.mff.longRows.toLocaleString("en")} rows pivoted to one column per variable. ` : ""}
          Showing {Math.min(PREVIEW_ROWS, table.rows.length)} of {table.rows.length.toLocaleString("en")} rows{table.truncated ? " read so far (the whole file is checked after upload)" : ""}. Nothing has left your computer yet.
        </p>
      </div>
      {series.length ? (
        <div className="grid gap-2">
          <h3 className="text-[13px] font-semibold">The series you picked{dateCol ? ", added up per date" : ""}</h3>
          <ul className="grid gap-1.5">
            {series.map((s) => {
              const values = seriesFor(table, s.column, dateCol);
              return (
                <li key={s.label} className="flex items-center justify-between gap-3 rounded-md border px-3 py-1.5">
                  <span className="min-w-0 truncate text-xs">{s.label}</span>
                  <Sparkline values={values} label={`${s.label}: ${values.length} points`} />
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

// ----------------------------------------------------------------------------- the step

type Phase = { kind: "idle" } | { kind: "uploading"; progress: UploadProgress | null } | { kind: "ingesting" } | { kind: "saving" } | { kind: "error"; message: string };

function freeName(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base;
  for (let i = 2; i < 100; i++) {
    const n = `${base.slice(0, 120)}-${i}`;
    if (!taken.has(n)) return n;
  }
  return `${base.slice(0, 100)}-${Date.now().toString(36)}`;
}

export function NewDataStep({ template, onYaml }: { template: GuidedTemplate; onYaml?: (yaml: string | null) => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const scope = useProjectScope();
  const grants = useGrants();
  const datasets = useDatasets();
  const specs = useSpecs();
  const local = useMemo(() => loadLocalDraft(template.id), [template.id]);
  const [name, setName] = useState(local?.name ?? defaultSessionName(template.id));
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<UploadFormat | null>(null);
  const [table, setTable] = useState<LocalTable | null>(null);
  const [parquetNames, setParquetNames] = useState("");
  const [answers, setAnswers] = useState<Answers>(() => ({ ...(local?.answers ?? {}) }));
  const touched = useRef(new Set<string>(Object.keys(local?.answers ?? {})));
  const [preset, setPreset] = useState<PresetMatch | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const keyRef = useRef(newIdempotencyKey());
  const canSave = can(grants, "create", "dataset") && can(grants, "create", "spec");

  const profiles = useMemo<ColumnProfile[] | null>(() => {
    if (table) return profileTable(table);
    if (format === "parquet") {
      const p = profilesFromNames(parquetNames.split(/[\n,]/));
      return p.length ? p : null;
    }
    return null;
  }, [table, format, parquetNames]);
  const detect = useDetectRoles(template.id, profiles);

  // Fill every question the user has not answered themselves: preset first, then detection.
  useEffect(() => {
    if (!detect.data || !profiles) return;
    const names = new Set(profiles.map((p) => p.name));
    setAnswers((prev) => {
      const next = { ...prev };
      for (const s of detect.data.questions) {
        if (touched.current.has(s.question_id) || s.answer === undefined || s.answer === null) continue;
        next[s.question_id] = s.answer;
      }
      for (const [qid, v] of Object.entries(preset?.answers ?? {})) {
        if (touched.current.has(qid) || !template.questions.some((q) => q.id === qid)) continue;
        const ok = Array.isArray(v) ? v.every((c) => names.has(c)) : names.has(v);
        if (ok) next[qid] = v;
      }
      // answers naming columns this file does not have are dropped
      for (const q of template.questions) {
        const v = next[q.id];
        if (q.kind === "column" && typeof v === "string" && v && !names.has(v)) delete next[q.id];
        if (q.kind === "columns" && Array.isArray(v)) next[q.id] = (v as string[]).filter((c) => names.has(c));
      }
      return next;
    });
  }, [detect.data, preset, profiles, template.questions]);

  useEffect(() => {
    saveLocalDraft(template.id, { name, answers });
  }, [template.id, name, answers]);

  const choose = async (f: File) => {
    setPhase({ kind: "idle" });
    const fmt = formatFromName(f.name);
    setFile(f);
    setPreset(null);
    if (fmt === "csv") {
      const t = await readLocalTable(f);
      setTable(t);
      setFormat(t.mff ? "mff" : "csv");
      const prof = profileTable(t);
      setPreset(t.mff ? detectMff(t) : detectMeridian(prof));
    } else {
      setTable(null);
      setFormat(fmt);
    }
  };

  const set = (qid: string, v: unknown) => {
    touched.current.add(qid);
    setAnswers((a) => ({ ...a, [qid]: v }));
  };

  const slug = slugify(name);
  const datasetName = `${slug}-data`;
  const missing = missingRequired(template, answers);
  const ready = Boolean(profiles?.length) && missing.length === 0 && Boolean(format);
  const body = useMemo<GuidedAnswers | null>(
    () => (ready && format ? { template_id: template.id, name: slug, dataset: datasetName, format, answers: normalizeAnswers(template, answers), columns: profiles ?? [] } : null),
    [ready, format, template, slug, datasetName, answers, profiles],
  );
  const draft = useGuidedDraft(useDebounced(body, 300));
  const draftError = draft.error instanceof ApiError ? draft.error : null;
  const errorFor = (qid: string): string | null => {
    const issue = draftError?.problem?.errors?.find((e) => e.pointer === `/answers/${qid}`);
    if (issue) return issue.message.charAt(0).toUpperCase() + issue.message.slice(1) + ".";
    return null;
  };
  const yaml = body && !draftError ? (draft.data?.yaml ?? null) : null;
  useEffect(() => {
    onYaml?.(yaml);
  }, [yaml, onYaml]);
  const warnings = (draft.data?.issues ?? []).filter((i) => i.severity !== "info");
  const problems = table ? tableProblems(table) : [];
  const busy = phase.kind === "uploading" || phase.kind === "ingesting" || phase.kind === "saving";

  const save = async () => {
    if (!file || !format || !body || !draft.data) return;
    try {
      setPhase({ kind: "uploading", progress: null });
      const takenSpecs = new Set((specs.data?.items ?? []).map((s) => s.name));
      const takenData = new Set((datasets.data?.items ?? []).map((d) => d.name));
      const specNameFree = freeName(slug, takenSpecs);
      const dsName = freeName(`${specNameFree}-data`, takenData);
      // The final draft names the dataset and spec that will really exist.
      const final: GuidedDraft =
        specNameFree === body.name && dsName === body.dataset
          ? draft.data
          : await unwrap(api.POST("/v1/guided/drafts", { body: { ...body, name: specNameFree, dataset: dsName }, headers: scope.headers }));
      const ds = await unwrap(
        api.POST("/v1/datasets", {
          body: { name: dsName, description: `Uploaded in Guided mode (${template.title}) from ${file.name}`.slice(0, 4000), labels: { [GUIDED_LABEL]: template.id } },
          headers: scope.headers,
        }),
      );
      const session = await createUpload(ds.id, {
        filename: file.name.replace(/[/\\\u0000-\u001f]/g, "_"),
        content_type: file.type || (format === "parquet" ? "application/vnd.apache.parquet" : "text/csv"),
        size_bytes: file.size,
      });
      await putToSignedUrl(session, file, (p) => setPhase({ kind: "uploading", progress: p }));
      setPhase({ kind: "ingesting" });
      const version: DatasetVersion = await unwrap(
        api.POST("/v1/datasets/{dataset_id}/versions", {
          params: { path: { dataset_id: ds.id }, header: { "Idempotency-Key": keyRef.current } },
          body: { upload_id: session.id, mapping: final.mapping, message: `Guided upload of ${file.name}`.slice(0, 2000) },
          headers: scope.headers,
        }),
      );
      setPhase({ kind: "saving" });
      const spec = await unwrap(
        api.POST("/v1/specs", { body: { yaml: final.yaml, message: `${GUIDED_MESSAGE} drafted from the ${template.title} template (data ${version.ref})` }, headers: scope.headers }),
      );
      clearLocalDraft(template.id);
      await Promise.all([qc.invalidateQueries({ queryKey: qk.specs(scope.key) }), qc.invalidateQueries({ queryKey: qk.datasets(scope.key) })]);
      const next = hasTask(template, "business_priors") ? "priors" : "run";
      void navigate({ to: "/guided/$specId/$step", params: { specId: spec.id, step: next } });
    } catch (e) {
      keyRef.current = newIdempotencyKey();
      setPhase({ kind: "error", message: errorMessage(e) });
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) void choose(f);
  };

  const columnQs = template.questions.filter((q) => q.kind === "column" || q.kind === "columns");
  const basicOther = template.questions.filter((q) => q.kind !== "column" && q.kind !== "columns" && q.ui_level !== "advanced");
  const advancedOther = template.questions.filter((q) => q.kind !== "column" && q.kind !== "columns" && q.ui_level === "advanced");
  const suggestionFor = (qid: string) => detect.data?.questions.find((s) => s.question_id === qid);
  const taken = new Set(Object.values(answers).flatMap((v) => (typeof v === "string" ? [v] : Array.isArray(v) ? (v as string[]) : [])));

  return (
    <div className="grid gap-6">
      <Card>
        <CardContent className="grid gap-4 pt-5">
          <Field label="Name this analysis" htmlFor="guided-name" hint={slug !== name ? <>Saved as <Mono>{slug}</Mono></> : "Lower-case letters, digits and dashes."}>
            <Input id="guided-name" value={name} onChange={(e) => setName(e.target.value)} className="max-w-md" maxLength={100} />
          </Field>
          {!file ? (
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              className={cn("flex flex-col items-center gap-3 rounded-xl border-2 border-dashed p-8 text-center sm:p-10", dragging && "border-primary bg-accent")}
            >
              <UploadCloud aria-hidden className="size-7 text-muted-foreground" />
              <div className="grid gap-1">
                <p className="font-medium">Drop your spreadsheet here</p>
                <p className="max-w-[52ch] text-[13px] text-muted-foreground">
                  A CSV file with one row per week (and market), or a Parquet file. MFF long tables and Google Meridian tables are recognised automatically. Your file is previewed here first; nothing is uploaded until you say so.
                </p>
              </div>
              <Button variant="outline" onClick={() => inputRef.current?.click()}>
                <FileUp /> Choose a file
              </Button>
              <input
                ref={inputRef}
                type="file"
                className="sr-only"
                aria-label="Choose a data file"
                accept=".csv,.tsv,.txt,.parquet,.pq"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void choose(f);
                }}
              />
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3 rounded-lg border p-3">
              <FileSpreadsheet aria-hidden className="size-4 text-muted-foreground" />
              <Mono className="min-w-0 break-all">{file.name}</Mono>
              <span className="text-xs text-muted-foreground">{formatBytes(file.size)}</span>
              {format ? <Badge variant="outline">{format === "mff" ? "MFF" : format.toUpperCase()}</Badge> : <Badge variant="danger">not a CSV or Parquet file</Badge>}
              {preset ? (
                <Badge variant="info" data-testid="preset">
                  <Sparkles aria-hidden /> {preset.label} recognised
                </Badge>
              ) : null}
              {!busy ? (
                <Button variant="ghost" size="sm" className="ml-auto" onClick={() => inputRef.current?.click()}>
                  Choose another file
                </Button>
              ) : null}
              <input
                ref={inputRef}
                type="file"
                className="sr-only"
                aria-label="Choose another data file"
                accept=".csv,.tsv,.txt,.parquet,.pq"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void choose(f);
                }}
              />
            </div>
          )}
          {file && !format ? <p role="alert" className="text-[13px] text-destructive">Guided mode reads CSV and Parquet files. Save your spreadsheet as CSV and choose it again.</p> : null}
          {format === "parquet" ? (
            <Field
              label="Which columns does the file have?"
              htmlFor="parquet-cols"
              hint="Parquet files are only opened inside the secure sandbox, so type the column names, one per line (or copy them from your tool)."
            >
              <Textarea id="parquet-cols" value={parquetNames} onChange={(e) => setParquetNames(e.target.value)} placeholder={"week\nmarket\nrevenue\ntv_spend"} className="max-w-md font-mono" />
            </Field>
          ) : null}
          {problems.length ? (
            <ul role="alert" className="grid gap-1 text-[13px] text-destructive">
              {problems.map((p) => (
                <li key={p} className="flex gap-2">
                  <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
                  {p}
                </li>
              ))}
            </ul>
          ) : null}
        </CardContent>
      </Card>

      {profiles?.length ? (
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <section aria-labelledby="questions-h" className="grid gap-5">
            <div className="grid gap-1">
              <h2 id="questions-h" className="text-[15px] font-semibold tracking-tight">
                A few questions about your columns
              </h2>
              <p className="text-[13px] text-muted-foreground">{detect.isPending ? "Looking at your columns…" : "We filled in our best guesses. Check each one; change anything that is wrong."}</p>
            </div>
            {columnQs.map((q) =>
              q.kind === "column" ? (
                <ColumnQuestion key={q.id} q={q} value={answers[q.id]} onChange={(v) => set(q.id, v)} profiles={profiles} suggestion={suggestionFor(q.id)} error={errorFor(q.id)} />
              ) : (
                <ColumnsQuestion key={q.id} q={q} value={answers[q.id]} onChange={(v) => set(q.id, v)} profiles={profiles} suggestion={suggestionFor(q.id)} error={errorFor(q.id)} taken={taken} />
              ),
            )}
            {basicOther.map((q) => (
              <OtherQuestion key={q.id} q={q} value={answers[q.id] ?? q.default} onChange={(v) => set(q.id, v)} error={errorFor(q.id)} />
            ))}
            {advancedOther.length ? (
              <details className="group rounded-lg border px-4 py-3">
                <summary className="cursor-pointer text-[13px] font-medium">More options</summary>
                <div className="grid gap-4 pt-4">
                  {advancedOther.map((q) => (
                    <OtherQuestion key={q.id} q={q} value={answers[q.id] ?? q.default} onChange={(v) => set(q.id, v)} error={errorFor(q.id)} />
                  ))}
                </div>
              </details>
            ) : null}
          </section>
          <aside aria-label="Preview" className="grid gap-4 lg:sticky lg:top-16">
            {table ? <DataPreview table={table} template={template} answers={answers} /> : null}
            <div className="grid gap-2 rounded-lg border bg-muted/30 p-4" aria-live="polite">
              {missing.length ? (
                <p className="flex gap-2 text-[13px]">
                  <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <span>
                    Still to answer: {missing.map((q) => q.prompt_plain.replace(/\?$/, "")).join("; ")}.
                  </span>
                </p>
              ) : draftError ? (
                <p className="flex gap-2 text-[13px] text-destructive">
                  <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
                  <span>{draftError.problem?.errors?.[0]?.message ?? errorMessage(draftError)}</span>
                </p>
              ) : draft.data ? (
                <p className="flex gap-2 text-[13px]">
                  <CheckCircle2 aria-hidden className="mt-0.5 size-4 shrink-0 text-success" />
                  <span>Everything the model needs is answered.</span>
                </p>
              ) : (
                <p className="text-[13px] text-muted-foreground">Checking your answers…</p>
              )}
              {warnings.length ? (
                <ul className="grid gap-1 text-xs text-muted-foreground">
                  {warnings.map((w, i) => (
                    <li key={i} className="flex gap-2">
                      <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" />
                      {w.message.charAt(0).toUpperCase() + w.message.slice(1)}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </aside>
        </div>
      ) : null}

      {phase.kind === "uploading" ? (
        <div className="grid gap-2" aria-live="polite">
          <p className="text-[13px]">Uploading your file…</p>
          <Progress label="Upload progress" value={phase.progress ? phase.progress.loaded / Math.max(1, phase.progress.total) : null} />
        </div>
      ) : null}
      {phase.kind === "ingesting" ? <p aria-live="polite" className="text-[13px]">Uploaded. Starting the secure check of your data…</p> : null}
      {phase.kind === "saving" ? <p aria-live="polite" className="text-[13px]">Saving your analysis…</p> : null}
      {phase.kind === "error" ? (
        <p role="alert" className="text-[13px] text-destructive">
          That did not work: {phase.message}
        </p>
      ) : null}

      <StepActions>
        <Button variant="ghost" onClick={() => void navigate({ to: "/guided" })}>
          Back to templates
        </Button>
        <div className="flex flex-wrap items-center gap-3">
          {!canSave ? <span className="text-xs text-muted-foreground">Your role can look but not upload. Ask a project admin for editor access.</span> : null}
          <Button onClick={() => void save()} disabled={!draft.data || !!draftError || busy || problems.length > 0 || !canSave || draft.isFetching}>
            <UploadCloud /> Upload and continue
          </Button>
        </div>
      </StepActions>
    </div>
  );
}
