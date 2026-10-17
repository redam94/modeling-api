import { FileUp, Plus, Trash2, UploadCloud } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";

import { errorMessage, newIdempotencyKey } from "@/api/client";
import { createUpload, useCreateDatasetVersion } from "@/api/queries";
import type { DatasetVersion, IngestMapping } from "@/api/types";
import { putToSignedUrl, type UploadProgress } from "@/api/upload";
import { Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Field, Input, NativeSelect, Progress } from "@/components/ui/primitives";
import { formatBytes } from "@/lib/format";
import { parseCsv, type CsvPreview } from "@/lib/csv";
import { cn } from "@/lib/utils";

import {
  formatFromFilename,
  initialMappings,
  ROLES,
  safeName,
  toIngestMapping,
  validateMappings,
  type ColumnMapping,
  type DimKind,
  type DimPolicy,
} from "./mapping";

const SAMPLE_BYTES = 256 * 1024;

type Phase = "pick" | "map" | "uploading" | "ingesting" | "done" | "error";

export function UploadWizard({ datasetId, onDone }: { datasetId: string; onDone: (v: DatasetVersion) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<IngestMapping["format"] | null>(null);
  const [preview, setPreview] = useState<CsvPreview | null>(null);
  const [mappings, setMappings] = useState<ColumnMapping[]>([]);
  const [message, setMessage] = useState("");
  const [phase, setPhase] = useState<Phase>("pick");
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const createVersion = useCreateDatasetVersion(datasetId);
  const keyRef = useRef(newIdempotencyKey());

  const choose = async (f: File) => {
    setError(null);
    const fmt = formatFromFilename(f.name);
    setFile(f);
    setFormat(fmt);
    if (fmt === "csv") {
      const text = await f.slice(0, SAMPLE_BYTES).text();
      const p = parseCsv(text, 20);
      setPreview(p);
      setMappings(initialMappings(p.header, p.rows));
    } else {
      setPreview(null);
      setMappings([]);
    }
    setPhase("map");
  };

  const tabular = format === "csv" || format === "parquet";
  const problems = tabular ? validateMappings(mappings) : [];

  const start = async () => {
    if (!file || !format) return;
    setError(null);
    setPhase("uploading");
    try {
      const session = await createUpload(datasetId, {
        filename: file.name.replace(/[/\\\u0000-\u001f]/g, "_"),
        content_type: file.type || "application/octet-stream",
        size_bytes: file.size,
      });
      await putToSignedUrl(session, file, setProgress);
      setPhase("ingesting");
      const version = await createVersion.mutateAsync({
        key: keyRef.current,
        body: {
          upload_id: session.id,
          mapping: tabular ? toIngestMapping(format, mappings) : { format },
          ...(message.trim() ? { message: message.trim() } : {}),
        },
      });
      setPhase("done");
      onDone(version);
    } catch (e) {
      setError(errorMessage(e));
      setPhase("error");
      keyRef.current = newIdempotencyKey();
    }
  };

  const update = (i: number, patch: Partial<ColumnMapping>) =>
    setMappings((ms) => ms.map((m, j) => (j === i ? { ...m, ...patch } : m)));

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) void choose(f);
  };

  return (
    <div className="grid gap-5">
      {phase === "pick" || !file ? (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            "flex flex-col items-center gap-3 rounded-lg border-2 border-dashed p-10 text-center",
            dragging && "border-primary bg-accent",
          )}
        >
          <UploadCloud className="size-6 text-muted-foreground" />
          <p className="max-w-[48ch] text-muted-foreground">
            Drop a CSV, Parquet, zarr zip or netCDF file here. For a CSV file, this page shows a preview in your browser before the upload.
          </p>
          <Button variant="outline" onClick={() => inputRef.current?.click()}>
            <FileUp /> Choose file
          </Button>
          <input
            ref={inputRef}
            type="file"
            className="sr-only"
            accept=".csv,.tsv,.txt,.parquet,.pq,.zip,.nc,.nc4"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void choose(f);
            }}
          />
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border p-3">
          <FileUp className="size-4 text-muted-foreground" />
          <Mono>{file.name}</Mono>
          <span className="text-xs text-muted-foreground">{formatBytes(file.size)}</span>
          {format ? <Badge variant="outline">{format}</Badge> : <Badge variant="danger">unsupported type</Badge>}
          {phase === "map" || phase === "error" ? (
            <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setPhase("pick")}>
              Choose another
            </Button>
          ) : null}
        </div>
      )}

      {preview && phase === "map" ? (
        <div className="grid gap-2">
          <p className="text-xs text-muted-foreground">
            Local preview: first {preview.rows.length} rows{preview.truncated ? " (the file has more rows)" : ""}. Delimiter:{" "}
            <Mono>{preview.delimiter === "\t" ? "tab" : preview.delimiter}</Mono>.
          </p>
          <div className="max-h-56 overflow-auto rounded-lg border">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-card">
                <tr className="border-b">
                  {preview.header.map((h, i) => (
                    <th key={i} className="px-2 py-1.5 text-left font-medium text-muted-foreground">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((r, i) => (
                  <tr key={i} className="border-b last:border-0">
                    {preview.header.map((_, j) => (
                      <td key={j} className="px-2 py-1 font-mono whitespace-nowrap">{r[j] ?? ""}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {tabular && (phase === "map" || phase === "error") ? (
        <div className="grid gap-2">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold">Column mapping</h3>
            {format === "parquet" ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() => setMappings((ms) => [...ms, { ...initialMappings(["column"], [])[0]!, column: "", name: "" }])}
              >
                <Plus /> Add column
              </Button>
            ) : null}
          </div>
          {format === "parquet" ? (
            <p className="text-xs text-muted-foreground">Only the sandboxed ingest run can read Parquet files. Type the column names yourself.</p>
          ) : null}
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  <th className="px-2 py-2 text-left font-medium">Column</th>
                  <th className="px-2 py-2 text-left font-medium">Use as</th>
                  <th className="px-2 py-2 text-left font-medium">Name</th>
                  <th className="px-2 py-2 text-left font-medium">Details</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {mappings.map((m, i) => (
                  <tr key={i} className="border-b last:border-0 align-top">
                    <td className="px-2 py-1.5">
                      {format === "parquet" ? (
                        <Input aria-label="Column name in the file" value={m.column} onChange={(e) => update(i, { column: e.target.value, name: safeName(e.target.value) })} className="h-8" />
                      ) : (
                        <Mono>{m.column}</Mono>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      <NativeSelect aria-label={`Use ${m.column} as`} value={m.as} onChange={(e) => update(i, { as: e.target.value as ColumnMapping["as"] })} className="h-8 w-28">
                        <option value="dim">dim</option>
                        <option value="var">variable</option>
                        <option value="ignore">ignore</option>
                      </NativeSelect>
                    </td>
                    <td className="px-2 py-1.5">
                      <Input aria-label={`Name for ${m.column}`} value={m.name} disabled={m.as === "ignore"} onChange={(e) => update(i, { name: e.target.value })} className="h-8 w-40 font-mono" />
                    </td>
                    <td className="px-2 py-1.5">
                      {m.as === "dim" ? (
                        <div className="flex flex-wrap gap-1.5">
                          <NativeSelect aria-label="Dim kind" value={m.dimKind} onChange={(e) => update(i, { dimKind: e.target.value as DimKind })} className="h-8 w-32">
                            {(["categorical", "time", "integer", "float", "index"] as const).map((k) => <option key={k}>{k}</option>)}
                          </NativeSelect>
                          <NativeSelect aria-label="Score-time policy" value={m.policy} onChange={(e) => update(i, { policy: e.target.value as DimPolicy })} className="h-8 w-32">
                            <option value="fixed">fixed</option>
                            <option value="extend">extend</option>
                            <option value="new_levels">new levels</option>
                          </NativeSelect>
                          {m.dimKind === "time" ? (
                            <Input aria-label="Frequency" placeholder="freq (D, W-SUN)" value={m.freq} onChange={(e) => update(i, { freq: e.target.value })} className="h-8 w-28" />
                          ) : null}
                        </div>
                      ) : m.as === "var" ? (
                        <div className="flex flex-wrap gap-1.5">
                          <NativeSelect aria-label="Role" value={m.role} onChange={(e) => update(i, { role: e.target.value as ColumnMapping["role"] })} className="h-8 w-32">
                            {ROLES.map((r) => <option key={r}>{r}</option>)}
                          </NativeSelect>
                          <Input aria-label="Units" placeholder="units" value={m.units} onChange={(e) => update(i, { units: e.target.value })} className="h-8 w-24" />
                          <NativeSelect aria-label="Missing values" value={m.missing} onChange={(e) => update(i, { missing: e.target.value as ColumnMapping["missing"] })} className="h-8 w-28">
                            <option value="forbid">forbid NaN</option>
                            <option value="allow">allow NaN</option>
                            <option value="mask">mask NaN</option>
                          </NativeSelect>
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">not ingested</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      {format === "parquet" ? (
                        <Button variant="ghost" size="icon-sm" aria-label="Remove column" onClick={() => setMappings((ms) => ms.filter((_, j) => j !== i))}>
                          <Trash2 />
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {problems.length ? (
            <ul className="grid gap-0.5 text-xs text-destructive">
              {problems.map((p, i) => <li key={i}>{p.message}</li>)}
            </ul>
          ) : null}
        </div>
      ) : null}

      {file && format && (phase === "map" || phase === "error") ? (
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Version message (optional)" htmlFor="version-msg" className="min-w-64 flex-1">
            <Input id="version-msg" value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Q3 refresh" />
          </Field>
          <Button onClick={() => void start()} disabled={problems.length > 0}>
            <UploadCloud /> Upload and ingest
          </Button>
        </div>
      ) : null}

      {phase === "uploading" ? (
        <div className="grid gap-2">
          <p className="text-[13px]">The file uploads to quarantine storage…</p>
          <Progress label="Upload progress" value={progress ? progress.loaded / Math.max(1, progress.total) : null} />
        </div>
      ) : null}
      {phase === "ingesting" ? <p className="text-[13px]">The upload is complete. The sandboxed ingest run starts…</p> : null}
      {error ? <p role="alert" className="text-[13px] text-destructive">{error}</p> : null}
    </div>
  );
}
