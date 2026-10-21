/**
 * Update numbers (MEI weekly-update layout): send the actual numbers of the last periods. The
 * browser reads the file or the pasted rows (`lib/actuals.ts`), shows the rows to edit and a
 * list of live checks; "Send" stays off until every check passes. The server checks everything
 * again and makes a readout: it compares the numbers with what the model expected (or, for a
 * model that can use them, updates its forecast). The model itself does not change.
 *
 * When the analyst allows it, the sender can also ask the analyst to refit the model with these
 * numbers. That only makes a request: the analyst decides, and only the analyst starts a run.
 */
import { Check, Download, Plus, Trash2, X } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { FileDrop, InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Input, Label, Textarea } from "@/components/ui/primitives";
import { cellText, parseCell, parsePasted, tableToXr, type Cell } from "@/lib/actuals";
import { cn } from "@/lib/utils";

import { dashboardErrorMessage, problemOf, useActualsTemplate, useReadout, useSubmitActuals } from "../client";
import { MAX_ACTUALS_ROWS, type ActualsSubmit, type ActualsTemplate, type ClientDashboard, type DashboardClient, type ReadoutHeader } from "../types";
import { ReadoutState, Section, saveText, Tags, Working } from "./parts";
import { actualsChecks, checksPass, filledRows, historyRow, periodOf, plural } from "./readout-model";

const MODE_NOTE: Record<ActualsTemplate["mode"], string> = {
  compared: "We compare your numbers with what the model expected. The model does not change.",
  conditioned: "The model uses your numbers to update its forecast. The model itself does not change.",
};

function initialRows(t: ActualsTemplate): Cell[][] {
  try {
    const rows = parsePasted(t.input_table, t.example_csv);
    if (rows.length) return rows;
  } catch {
    // An example that does not parse: start with empty rows.
  }
  const n = (t.input_table.columns ?? []).length;
  return Array.from({ length: 4 }, () => Array.from({ length: n }, () => null));
}

function RowsEditor({ template, rows, onChange, disabled }: { template: ActualsTemplate; rows: Cell[][]; onChange: (rows: Cell[][]) => void; disabled: boolean }) {
  const cols = template.input_table.columns ?? [];
  const set = (i: number, j: number, text: string) => {
    const col = cols[j];
    if (!col) return;
    onChange(rows.map((r, k) => (k === i ? r.map((c, m) => (m === j ? parseCell(text, col) : c)) : r)));
  };
  return (
    <div className="grid gap-2">
      <div className="max-w-full overflow-x-auto rounded-lg border" data-slot="scroll-x">
        <table className="w-max min-w-full border-collapse text-sm">
          <caption className="sr-only">Your numbers, one row a period</caption>
          <thead>
            <tr className="border-b bg-muted/50 text-left text-xs text-hint">
              <th scope="col" className="px-3 py-2 font-medium">
                Row
              </th>
              {cols.map((c) => (
                <th key={c.name} scope="col" className={cn("px-3 py-2 font-medium", c.name === template.kpi_column && "text-foreground")} title={c.description}>
                  {c.name}
                </th>
              ))}
              <th scope="col" className="px-3 py-2">
                <span className="sr-only">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-b last:border-b-0">
                <th scope="row" className="px-3 py-1 text-xs font-normal text-hint tabular">
                  {i + 1}
                </th>
                {cols.map((c, j) => (
                  <td key={c.name} className="px-1 py-1">
                    <Input
                      aria-label={`${c.name}, row ${i + 1}`}
                      value={cellText(r[j])}
                      onChange={(e) => set(i, j, e.target.value)}
                      disabled={disabled}
                      inputMode={c.role === "coord" ? "text" : "decimal"}
                      className="h-8 w-32 tabular"
                    />
                  </td>
                ))}
                <td className="px-1 py-1">
                  <Button variant="ghost" size="icon-sm" aria-label={`Remove row ${i + 1}`} disabled={disabled} onClick={() => onChange(rows.filter((_, k) => k !== i))}>
                    <Trash2 />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <Button
          variant="action"
          size="sm"
          disabled={disabled || rows.length >= (template.max_rows ?? MAX_ACTUALS_ROWS)}
          onClick={() => onChange([...rows, cols.map(() => null)])}
        >
          <Plus /> Add a row
        </Button>
      </div>
    </div>
  );
}

function SentReadout({ client, header }: { client: DashboardClient; header: ReadoutHeader }) {
  const q = useReadout(client, header.seq);
  const r = q.data ?? null;
  const row = historyRow(r ?? header);
  return (
    <div className="grid gap-2 rounded-lg border p-4" data-readout="sent">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-semibold text-foreground">{row.title}</span>
        <Tags tags={row.tags} />
      </div>
      {r?.status === "ready" && r.snapshot ? <p className="text-sm text-foreground">{r.snapshot.lead}</p> : null}
      <ReadoutState readout={r ?? { status: header.status, problem: null, refit_status: header.refit_status ?? null }} />
    </div>
  );
}

export function UpdateNumbers({ client, dashboard, preview = false }: { client: DashboardClient; dashboard: ClientDashboard; preview?: boolean }) {
  const tq = useActualsTemplate(client);
  if (tq.isPending) return <Working>Loading the template…</Working>;
  if (tq.isError) return <InlineMessage tone="error">{dashboardErrorMessage(tq.error)}</InlineMessage>;
  return <UpdateForm client={client} dashboard={dashboard} template={tq.data} preview={preview} />;
}

function UpdateForm({ client, dashboard, template, preview }: { client: DashboardClient; dashboard: ClientDashboard; template: ActualsTemplate; preview: boolean }) {
  const [rows, setRows] = useState<Cell[][]>(() => initialRows(template));
  const [paste, setPaste] = useState("");
  const [refit, setRefit] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [sent, setSent] = useState<ReadoutHeader | null>(null);
  const submit = useSubmitActuals(client);
  const pasteId = useId();
  const refitId = useId();
  const period = periodOf(dashboard);
  const checks = useMemo(() => actualsChecks(template, rows), [template, rows]);
  const allowRefit = Boolean(dashboard.inputs?.refit_requests);
  const paused = dashboard.live_inputs === "paused";
  const max = template.max_rows ?? MAX_ACTUALS_ROWS;

  const take = (text: string) => {
    setReadError(null);
    try {
      const next = parsePasted(template.input_table, text);
      if (!next.length) {
        setReadError("We found no rows. Put the column names in the first row and one period on each next row.");
        return;
      }
      setRows(next);
      setSent(null);
    } catch (err) {
      setReadError(err instanceof Error ? err.message : String(err));
    }
  };

  const send = () => {
    let inputs: ActualsSubmit["inputs"];
    try {
      inputs = tableToXr(template.input_table, filledRows(rows));
    } catch (err) {
      setReadError(err instanceof Error ? err.message : String(err));
      return;
    }
    setReadError(null);
    const body: ActualsSubmit = { inputs, ...(allowRefit && refit ? { request_refit: true } : {}) };
    submit.mutate({ body }, { onSuccess: (h) => setSent(h) });
  };

  const problem = submit.isError ? problemOf(submit.error) : null;
  const issues = problem?.problem?.errors ?? [];
  const ok = checksPass(checks);

  return (
    <div className="grid min-w-0 gap-6" data-tab="update">
      <InlineMessage tone="info" role="none">
        {MODE_NOTE[template.mode]}
      </InlineMessage>
      {paused ? <InlineMessage tone="warning">Sending numbers is paused on this dashboard. Your analyst is checking it.</InlineMessage> : null}
      <div className="grid min-w-0 gap-6 lg:grid-cols-12">
        <Section
          className="lg:col-span-8"
          title="Your numbers"
          description={`One row for each ${period}. Up to ${max} rows. The column ${template.kpi_column} holds the actual result.`}
          action={
            <Button variant="secondary" size="sm" onClick={() => saveText("template.csv", template.example_csv)}>
              <Download /> Download template (.csv)
            </Button>
          }
        >
          <FileDrop
            accept=".csv,text/csv,text/plain"
            label="Drop a .csv file here, or choose a file"
            hint={`Use the template's columns. Up to ${max} rows.`}
            disabled={preview}
            onFiles={(files) => {
              const f = files[0];
              if (f) void f.text().then(take, (e: unknown) => setReadError(String(e)));
            }}
          />
          <details className="grid gap-2">
            <summary className="cursor-pointer text-sm font-semibold text-primary">Paste from a spreadsheet</summary>
            <div className="grid gap-2 pt-2">
              <Label htmlFor={pasteId}>Rows with the column names first</Label>
              <Textarea id={pasteId} value={paste} onChange={(e) => setPaste(e.target.value)} rows={5} className="font-mono text-xs" disabled={preview} />
              <div>
                <Button variant="secondary" size="sm" disabled={preview || !paste.trim()} onClick={() => take(paste)}>
                  Use the pasted rows
                </Button>
              </div>
            </div>
          </details>
          {readError ? <InlineMessage tone="error">{readError}</InlineMessage> : null}
          <RowsEditor
            template={template}
            rows={rows}
            onChange={(r) => {
              setRows(r);
              setSent(null);
            }}
            disabled={preview}
          />
        </Section>
        <div className="grid min-w-0 content-start gap-6 lg:col-span-4">
          <Section title="Checks">
            <ul aria-label="Checks" className="grid gap-2 text-sm">
              {checks.map((c) => (
                <li key={c.text} className="flex items-start gap-2" data-ok={c.ok}>
                  {c.ok ? <Check aria-hidden className="mt-0.5 size-4 flex-none text-success" /> : <X aria-hidden className="mt-0.5 size-4 flex-none text-destructive" />}
                  <span>
                    <span className="sr-only">{c.ok ? "Passed: " : "Fix: "}</span>
                    {c.text}
                  </span>
                </li>
              ))}
            </ul>
          </Section>
          <Section title="Send">
            {allowRefit ? (
              <div className="grid gap-1">
                <div className="flex items-start gap-2">
                  <Checkbox id={refitId} checked={refit} onCheckedChange={(v) => setRefit(v === true)} disabled={preview} aria-describedby={`${refitId}-hint`} className="mt-0.5" />
                  <Label htmlFor={refitId} className="leading-snug">
                    Ask the analyst to refit with these numbers
                  </Label>
                </div>
                <p id={`${refitId}-hint`} className="pl-6 text-xs text-hint">
                  The analyst decides if the model is fitted again. Only the analyst can start a refit.
                </p>
              </div>
            ) : null}
            <div>
              <Button onClick={send} disabled={preview || paused || !ok || submit.isPending}>
                Send
              </Button>
            </div>
            {preview ? <p className="text-xs text-hint">Inputs are off in the preview.</p> : null}
            {submit.isPending ? <Working>Sending your numbers…</Working> : null}
            {submit.isError ? (
              <InlineMessage tone="error">
                {dashboardErrorMessage(submit.error)}
                {issues.length ? (
                  <ul className="mt-1 list-disc pl-4">
                    {issues.slice(0, 8).map((i, k) => (
                      <li key={k}>{i.message}</li>
                    ))}
                  </ul>
                ) : null}
              </InlineMessage>
            ) : null}
            {sent ? (
              <div className="grid gap-3">
                <InlineMessage tone="success">
                  We received your numbers. The new readout appears in History when it is ready.
                  {sent.visibility === "pending_review" ? " The analyst checks it before other people see it." : ""}
                </InlineMessage>
                <SentReadout client={client} header={sent} />
              </div>
            ) : null}
          </Section>
        </div>
      </div>
      <p className="text-xs text-hint">
        We send only the {filledRows(rows).length} {plural("row", filledRows(rows).length)} with values. Blank rows are left out.
      </p>
    </div>
  );
}
