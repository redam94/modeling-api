/**
 * Score a file (v0.17 batch scoring, plan §3.4): the viewer sends a CSV file of new cases and
 * gets a summary of the predictions and a CSV of them to download. The browser reads the file
 * itself (`lib/csv`), shows the first rows as text and checks the columns, the row cap, the ids
 * and each value against the template before anything is sent; the server checks everything
 * again and makes a private readout. "Download predictions" fetches the CSV through the
 * viewer's dashboard client (the share session header, never a cookie) and saves it from a Blob.
 */
import { Download } from "lucide-react";
import { useMemo, useState } from "react";

import { FileDrop, InlineMessage } from "@/components/ods";
import { grouped } from "@/components/ods/format";
import { SmallTable } from "@/components/readout-charts-extra";
import { Button } from "@/components/ui/button";
import { parseCsv } from "@/lib/csv";

import { dashboardErrorMessage, problemOf, useBatchTemplate, useReadout, useSubmitBatch } from "../client";
import { MAX_BATCH_ROWS, type BatchTemplate, type ClientDashboard, type DashboardClientV2 } from "../types";
import { batchChecks, batchRows, batchToXr } from "./panels/inputs-model";
import { ChecksList } from "./panels/checks-list";
import { BatchView } from "./panels/results";
import { ReadoutState, Section, saveText, Working } from "./parts";
import { checksPass, plural } from "./readout-model";

/** `MAX_XRJSON_BYTES`: the largest inline dataset the server takes. */
const MAX_BODY_BYTES = 5 * 1024 * 1024;
const PREVIEW_ROWS = 20;

interface FileState {
  name: string;
  header: string[];
  /** The raw cells of the rows read (≤ max rows + 1), in the file's column order. */
  raw: string[][];
  /** All data rows in the file (an estimate when the file is larger than the cap). */
  total: number;
}

function Scored({ client, dashboard, seq }: { client: DashboardClientV2; dashboard: ClientDashboard; seq: number }) {
  const q = useReadout(client, seq);
  if (q.isError) return <InlineMessage tone="error">{dashboardErrorMessage(q.error)}</InlineMessage>;
  const r = q.data;
  if (!r) return <Working>Scoring the file…</Working>;
  return (
    <div className="grid gap-3" data-readout="scored">
      <ReadoutState readout={r} />
      {r.status === "ready" && r.snapshot?.batch ? <BatchView client={client} dashboard={dashboard} readout={r} summary={r.snapshot.batch} /> : null}
    </div>
  );
}

export function ScoreFile({ client, dashboard, preview = false }: { client: DashboardClientV2; dashboard: ClientDashboard; preview?: boolean }) {
  const tq = useBatchTemplate(client);
  if (tq.isPending) return <Working>Loading the template…</Working>;
  if (tq.isError) return <InlineMessage tone="error">{dashboardErrorMessage(tq.error)}</InlineMessage>;
  return <ScoreForm client={client} dashboard={dashboard} template={tq.data} preview={preview} />;
}

function ScoreForm({ client, dashboard, template, preview }: { client: DashboardClientV2; dashboard: ClientDashboard; template: BatchTemplate; preview: boolean }) {
  const [file, setFile] = useState<FileState | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [seq, setSeq] = useState<number | null>(null);
  const [idemKey, setIdemKey] = useState(() => crypto.randomUUID());
  const submit = useSubmitBatch(client);
  const max = Math.min(template.max_rows, MAX_BATCH_ROWS);
  const paused = dashboard.live_inputs === "paused";

  const rows = useMemo(() => (file ? batchRows(template, file.header, file.raw) : []), [file, template]);
  const checks = useMemo(() => (file ? batchChecks({ ...template, max_rows: max }, file.header, rows, file.total) : []), [file, template, rows, max]);
  const ok = file !== null && checksPass(checks);

  const take = (f: File) => {
    setReadError(null);
    setSeq(null);
    submit.reset();
    setIdemKey(crypto.randomUUID());
    void f.text().then(
      (text) => {
        const csv = parseCsv(text, max + 1);
        if (!csv.header.length || !csv.rows.length) {
          setFile(null);
          setReadError("We found no rows. Put the column names in the first row and one case on each next row.");
          return;
        }
        const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "").length - 1;
        setFile({ name: f.name, header: csv.header, raw: csv.rows.slice(0, max + 1), total: csv.truncated ? Math.max(lines, max + 1) : csv.rows.filter((r) => r.some((c) => c.trim() !== "")).length });
      },
      (e: unknown) => setReadError(String(e)),
    );
  };

  const send = () => {
    const inputs = batchToXr(template, rows);
    if (new Blob([JSON.stringify(inputs)]).size > MAX_BODY_BYTES) {
      setReadError("The file is too large to send. Send fewer cases at a time.");
      return;
    }
    submit.mutate({ body: { inputs, label: file?.name.slice(0, 80) ?? null }, idemKey }, { onSuccess: (r) => setSeq(r.seq) });
  };

  const problem = submit.isError ? problemOf(submit.error) : null;
  const issues = problem?.problem?.errors ?? [];

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-12" data-tab="batch">
      <Section
        className="lg:col-span-8"
        title="Your file"
        description={`A .csv file with one case on each row and the columns of the template. Up to ${grouped(max)} rows.`}
        action={
          <Button variant="secondary" size="sm" onClick={() => saveText("batch-template.csv", template.example_csv)}>
            <Download /> Download template (.csv)
          </Button>
        }
      >
        {paused ? <InlineMessage tone="warning">Scoring files is paused on this dashboard. Your analyst is checking it.</InlineMessage> : null}
        <FileDrop
          accept=".csv,text/csv,text/plain"
          label="Drop a .csv file here, or choose a file"
          hint={`Use the template's columns: ${template.columns.map((c) => c.name).join(", ")}.`}
          disabled={preview}
          onFiles={(files) => {
            const f = files[0];
            if (f) take(f);
          }}
        />
        {readError ? <InlineMessage tone="error">{readError}</InlineMessage> : null}
        {file ? (
          <div className="grid gap-2">
            <p className="text-sm text-foreground">
              {file.name}: the first {Math.min(PREVIEW_ROWS, file.raw.length)} of {grouped(file.total)} {plural("row", file.total)}.
            </p>
            <SmallTable label={`The first rows of ${file.name}`} columns={file.header} rows={file.raw.slice(0, PREVIEW_ROWS)} />
          </div>
        ) : null}
      </Section>
      <div className="grid min-w-0 content-start gap-6 lg:col-span-4">
        {file ? (
          <Section title="Checks">
            <ChecksList checks={checks} />
          </Section>
        ) : null}
        <Section title="Score">
          <div>
            <Button onClick={send} disabled={preview || paused || !ok || submit.isPending || seq !== null}>
              Score the file
            </Button>
          </div>
          {preview ? <p className="text-xs text-hint">Inputs are off in the preview.</p> : null}
          <p className="text-xs text-hint">Only you and the analysts can see the predictions.</p>
          {submit.isPending ? <Working>Sending your file…</Working> : null}
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
          {seq !== null ? <Scored client={client} dashboard={dashboard} seq={seq} /> : null}
        </Section>
      </div>
    </div>
  );
}
