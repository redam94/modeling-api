/**
 * Check accuracy (v0.17, plan §3.3): how right the model was on the actual results, and a form
 * to send new ones. It generalises Update numbers by the template's `match`:
 *
 * - `time` (forecasts, the media planner): the v0.16 Update numbers form, unchanged;
 * - `row` (regression, classifier): one row per case, its id, the values the model uses and the
 *   actual result. The browser checks the rows, the ids and the results before anything is
 *   sent; the server matches each id to the predictions and measures the accuracy.
 *
 * The accuracy shown is the server's (the web recomputes no layout 2 metric).
 */
import { Download } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { FileDrop, InlineMessage } from "@/components/ods";
import { SmallTable } from "@/components/readout-charts-extra";
import { Button } from "@/components/ui/button";
import { Label, Textarea } from "@/components/ui/primitives";
import type { Cell } from "@/lib/actuals";
import { parseCsv } from "@/lib/csv";

import { dashboardErrorMessage, problemOf, useActualsTemplate, useReadout, useSubmitActuals } from "../client";
import { MAX_ROW_ACTUALS, type ActualsSubmit, type ActualsTemplate, type ClientDashboard, type DashboardClient, type ReadoutHeader } from "../types";
import { rowActualsChecks, rowActualsColumns, rowActualsRows, rowActualsToXr } from "./panels/inputs-model";
import { ChecksList } from "./panels/checks-list";
import { AccuracyDetailView, AccuracySection } from "./panels/results";
import { ReadoutState, Section, saveText, Tags, Working } from "./parts";
import { checksPass, historyRow, plural } from "./readout-model";
import { UpdateNumbers } from "./v1/update-numbers";

const MODE_NOTE: Record<ActualsTemplate["mode"], string> = {
  compared: "We compare the actual results with the predictions. The model does not change.",
  conditioned: "The model uses your numbers to update its forecast. The model itself does not change.",
};

function SentRows({ client, dashboard, header }: { client: DashboardClient; dashboard: ClientDashboard; header: ReadoutHeader }) {
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
      {r?.status === "ready" && r.snapshot?.accuracy_detail ? <AccuracyDetailView dashboard={dashboard} detail={r.snapshot.accuracy_detail} /> : null}
      <ReadoutState readout={r ?? { status: header.status, problem: null, refit_status: header.refit_status ?? null }} />
    </div>
  );
}

function RowForm({ client, dashboard, template, preview }: { client: DashboardClient; dashboard: ClientDashboard; template: ActualsTemplate; preview: boolean }) {
  const pasteId = useId();
  const refitId = useId();
  const [rows, setRows] = useState<Cell[][]>([]);
  const [paste, setPaste] = useState("");
  const [refit, setRefit] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [sent, setSent] = useState<ReadoutHeader | null>(null);
  const [idemKey, setIdemKey] = useState(() => crypto.randomUUID());
  const submit = useSubmitActuals(client);
  // One row per case in the template's own columns (its example CSV), not the time-shaped input table.
  const cols = useMemo(() => rowActualsColumns(template), [template]);
  const idColumn = template.id_column ?? cols[0] ?? "id";
  const outcome = template.label_column ?? template.kpi_column;
  const max = Math.min(template.max_rows ?? MAX_ROW_ACTUALS, MAX_ROW_ACTUALS);
  const checks = useMemo(() => rowActualsChecks(idColumn, outcome, cols, rows, max), [idColumn, outcome, cols, rows, max]);
  const allowRefit = Boolean(dashboard.inputs?.refit_requests);
  const paused = dashboard.live_inputs === "paused";
  const changed = () => {
    setSent(null);
    submit.reset();
    setIdemKey(crypto.randomUUID());
  };
  const take = (text: string) => {
    setReadError(null);
    try {
      const csv = parseCsv(text, max + 1);
      const next = csv.header.length ? rowActualsRows(cols, idColumn, csv.header, csv.rows, csv.delimiter === ";") : [];
      if (!next.length) {
        setReadError("We found no rows. Put the column names in the first row and one case on each next row.");
        return;
      }
      setRows(next);
      changed();
    } catch (err) {
      setReadError(err instanceof Error ? err.message : String(err));
    }
  };
  const send = () => {
    const inputs = rowActualsToXr(cols, idColumn, rows) as ActualsSubmit["inputs"];
    const body: ActualsSubmit = { inputs, ...(allowRefit && refit ? { request_refit: true } : {}) };
    submit.mutate({ body, idemKey }, { onSuccess: (h) => setSent(h) });
  };
  const issues = submit.isError ? (problemOf(submit.error)?.problem?.errors ?? []) : [];
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-12">
      <Section
        className="lg:col-span-8"
        title="The actual results"
        description={`One row for each case: its ${idColumn}, the values the model used and the actual result in ${outcome}. Up to ${max} rows.`}
        action={
          <Button variant="secondary" size="sm" onClick={() => saveText("actuals-template.csv", template.example_csv)}>
            <Download /> Download template (.csv)
          </Button>
        }
      >
        <InlineMessage tone="info" role="none">
          {MODE_NOTE[template.mode]}
        </InlineMessage>
        {paused ? <InlineMessage tone="warning">Sending results is paused on this dashboard. Your analyst is checking it.</InlineMessage> : null}
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
        {rows.length ? (
          <div className="grid gap-2">
            <p className="text-sm text-foreground">
              The first {Math.min(20, rows.length)} of {rows.length} {plural("row", rows.length)}.
            </p>
            <SmallTable label="The rows to send" columns={cols} rows={rows.slice(0, 20)} />
          </div>
        ) : null}
      </Section>
      <div className="grid min-w-0 content-start gap-6 lg:col-span-4">
        <Section title="Checks">
          <ChecksList checks={checks} />
        </Section>
        <Section title="Send">
          {allowRefit ? (
            <div className="flex items-start gap-2">
              <input
                id={refitId}
                type="checkbox"
                checked={refit}
                disabled={preview}
                onChange={(e) => {
                  setRefit(e.target.checked);
                  changed();
                }}
                className="mt-0.5 size-4 accent-primary"
              />
              <Label htmlFor={refitId} className="leading-snug">
                Ask the analyst to refit with these results
              </Label>
            </div>
          ) : null}
          <div>
            <Button onClick={send} disabled={preview || paused || !checksPass(checks) || submit.isPending || sent !== null}>
              Send
            </Button>
          </div>
          {preview ? <p className="text-xs text-hint">Inputs are off in the preview.</p> : null}
          {submit.isPending ? <Working>Sending the results…</Working> : null}
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
                We received the results. The accuracy appears here when it is ready.
                {sent.visibility === "pending_review" ? " The analyst checks it before other people see it." : ""}
              </InlineMessage>
              <SentRows client={client} dashboard={dashboard} header={sent} />
            </div>
          ) : null}
        </Section>
      </div>
    </div>
  );
}

export function CheckAccuracy({ client, dashboard, preview = false }: { client: DashboardClient; dashboard: ClientDashboard; preview?: boolean }) {
  const tq = useActualsTemplate(client);
  const snap = dashboard.latest_readout?.snapshot ?? null;
  const detail = snap?.accuracy_detail ?? null;
  const history = snap?.accuracy_history ?? [];
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6" data-tab="accuracy">
      <AccuracySection dashboard={dashboard} detail={detail} history={history} />
      {tq.isPending ? (
        <Working>Loading the template…</Working>
      ) : tq.isError ? (
        <InlineMessage tone="error">{dashboardErrorMessage(tq.error)}</InlineMessage>
      ) : tq.data.match === "row" ? (
        <RowForm client={client} dashboard={dashboard} template={tq.data} preview={preview} />
      ) : (
        <UpdateNumbers client={client} dashboard={dashboard} preview={preview} />
      )}
    </div>
  );
}
