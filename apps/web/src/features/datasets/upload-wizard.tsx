/**
 * Upload a new dataset version: one file (as before M4) or several files combined into one
 * version (M4: `IngestAssembly`). Each file has a format, a column mapping (CSV, Parquet) or
 * an array mapping (raw HDF5, from an inspect run), and a place in the tree: its node and how
 * it combines with the other files of that node. Files go straight to signed URLs; only the
 * sandboxed ingest (and inspect) runs read them.
 */
import { FileUp, Layers, Plus, Search, Trash2, UploadCloud } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";

import { errorMessage, newIdempotencyKey } from "@/api/client";
import { createUpload, useCreateDatasetVersion } from "@/api/queries";
import type { DatasetVersion, DatasetVersionCreate, IngestMapping, UploadInspection, UploadSession } from "@/api/types";
import { putToSignedUrl, type UploadProgress } from "@/api/upload";
import { Mono, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Field, Input, NativeSelect, Progress } from "@/components/ui/primitives";
import { useEffectivePersona } from "@/features/guided/persona";
import { formatBytes } from "@/lib/format";
import { parseCsv, type CsvPreview } from "@/lib/csv";
import { cn } from "@/lib/utils";

import { useStartInspect, useUploadInspection } from "./api";
import { ArrayBrowser, Hdf5Mapper } from "./inspection";
import {
  coordDrafts,
  DEFAULT_PLACEMENT,
  formatFromFilename,
  initialHdf5Vars,
  initialMappings,
  needsAssembly,
  ROLES,
  safeName,
  toAssembly,
  toHdf5Mapping,
  toIngestMapping,
  validateHdf5,
  validateMappings,
  validatePlacements,
  type ColumnMapping,
  type DimKind,
  type DimPolicy,
  type Hdf5CoordDraft,
  type Hdf5VarDraft,
  type Placement,
} from "./mapping";

const SAMPLE_BYTES = 256 * 1024;
const ACCEPT = ".csv,.tsv,.txt,.parquet,.pq,.zip,.nc,.nc4,.h5,.hdf5,.he5";

type Format = IngestMapping["format"];
type Phase = "pick" | "map" | "uploading" | "ingesting" | "done" | "error";

const FORMATS: { value: Format; label: string }[] = [
  { value: "csv", label: "CSV" },
  { value: "parquet", label: "Parquet" },
  { value: "zarr_zip", label: "zarr zip" },
  { value: "netcdf", label: "netCDF (or HDF5 written by xarray)" },
  { value: "hdf5", label: "raw HDF5 (map the arrays)" },
];

interface SourceUpload {
  session: UploadSession;
  /** The bytes are in quarantine storage. */
  done: boolean;
}

/** One file of the version being uploaded. */
interface Source {
  key: string;
  file: File;
  format: Format | null;
  preview: CsvPreview | null;
  mappings: ColumnMapping[];
  placement: Omit<Placement, "file">;
  upload: SourceUpload | null;
  /** The inspect run of the upload (raw HDF5: required; netCDF/zarr: optional). */
  inspectRunId: string | null;
  inspection: UploadInspection | null;
  hdf5Vars: Hdf5VarDraft[];
  hdf5Coords: Hdf5CoordDraft[];
}

let sourceSeq = 0;

async function newSource(file: File, format: Format | null): Promise<Source> {
  let preview: CsvPreview | null = null;
  let mappings: ColumnMapping[] = [];
  if (format === "csv") {
    const text = await file.slice(0, SAMPLE_BYTES).text();
    preview = parseCsv(text, 20);
    mappings = initialMappings(preview.header, preview.rows);
  }
  sourceSeq += 1;
  return { key: `src-${sourceSeq}`, file, format, preview, mappings, placement: { ...DEFAULT_PLACEMENT }, upload: null, inspectRunId: null, inspection: null, hdf5Vars: [], hdf5Coords: [] };
}

const tabularFormat = (f: Format | null) => f === "csv" || f === "parquet";
const cleanName = (name: string) => name.replace(/[/\\\u0000-\u001f]/g, "_");

/** The `IngestMapping` of one file. */
function sourceMapping(s: Source): IngestMapping {
  if (tabularFormat(s.format)) return toIngestMapping(s.format, s.mappings);
  if (s.format === "hdf5") return toHdf5Mapping(s.hdf5Vars, s.hdf5Coords);
  return { format: s.format! };
}

export function UploadWizard({ datasetId, onDone }: { datasetId: string; onDone: (v: DatasetVersion) => void }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [message, setMessage] = useState("");
  const [phase, setPhase] = useState<Phase>("pick");
  const [progress, setProgress] = useState<{ index: number; p: UploadProgress | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const createVersion = useCreateDatasetVersion(datasetId);
  const startInspect = useStartInspect(datasetId);
  const keyRef = useRef(newIdempotencyKey());
  const expert = useEffectivePersona() === "expert";

  const add = async (files: File[], replace: boolean) => {
    setError(null);
    const fresh = await Promise.all(files.map((f) => newSource(f, formatFromFilename(f.name))));
    setSources((cur) => (replace ? fresh : [...cur, ...fresh]));
    setPhase("map");
  };

  const patch = (key: string, p: Partial<Source> | ((s: Source) => Partial<Source>)) =>
    setSources((cur) => cur.map((s) => (s.key === key ? { ...s, ...(typeof p === "function" ? p(s) : p) } : s)));

  const setFormat = async (s: Source, format: Format) => {
    const next = await newSource(s.file, format);
    patch(s.key, { format, preview: next.preview, mappings: next.mappings, hdf5Vars: s.inspection ? initialHdf5Vars(s.inspection.arrays ?? []) : [], hdf5Coords: [] });
  };

  const placements: Placement[] = sources.map((s) => ({ file: s.file.name, ...s.placement }));
  const assembly = needsAssembly(placements);
  const placementProblems = assembly ? validatePlacements(placements) : [];
  const problemsBySource = useMemo(
    () =>
      new Map(
        sources.map((s) => [
          s.key,
          tabularFormat(s.format)
            ? validateMappings(s.mappings).map((p) => p.message)
            : s.format === "hdf5"
              ? s.inspection
                ? validateHdf5(s.hdf5Vars, s.hdf5Coords, s.inspection.arrays ?? []).map((p) => p.message)
                : ["List the arrays of the file first (Upload and list the arrays)."]
              : [],
        ]),
      ),
    [sources],
  );
  const blocking =
    !sources.length ||
    sources.some((s) => !s.format) ||
    placementProblems.length > 0 ||
    [...problemsBySource.values()].some((p) => p.length > 0);

  /** Create the upload session of a file and put its bytes (once). */
  const ensureUploaded = async (s: Source, index: number): Promise<string> => {
    if (s.upload?.done) return s.upload.session.id;
    const session =
      s.upload?.session ??
      (await createUpload(datasetId, { filename: cleanName(s.file.name), content_type: s.file.type || "application/octet-stream", size_bytes: s.file.size }));
    patch(s.key, { upload: { session, done: false } });
    setProgress({ index, p: null });
    await putToSignedUrl(session, s.file, (p) => setProgress({ index, p }));
    patch(s.key, { upload: { session, done: true } });
    return session.id;
  };

  const inspect = async (s: Source, index: number) => {
    setError(null);
    try {
      const uploadId = await ensureUploaded(s, index);
      setProgress(null);
      const run = await startInspect.mutateAsync(uploadId);
      patch(s.key, { inspectRunId: run.id, inspection: null });
    } catch (e) {
      setProgress(null);
      setError(errorMessage(e));
    }
  };

  const start = async () => {
    if (blocking) return;
    setError(null);
    setPhase("uploading");
    try {
      const ids: string[] = [];
      for (const [i, s] of sources.entries()) ids.push(await ensureUploaded(s, i));
      setPhase("ingesting");
      const body: DatasetVersionCreate = assembly
        ? { assembly: toAssembly(sources.map((s, i) => ({ uploadId: ids[i]!, mapping: sourceMapping(s), placement: placements[i]! }))) }
        : { upload_id: ids[0]!, mapping: sourceMapping(sources[0]!) };
      const version = await createVersion.mutateAsync({ key: keyRef.current, body: { ...body, ...(message.trim() ? { message: message.trim() } : {}) } });
      setPhase("done");
      onDone(version);
    } catch (e) {
      setError(errorMessage(e));
      setPhase("error");
      keyRef.current = newIdempotencyKey();
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const files = [...e.dataTransfer.files];
    if (files.length) void add(files, phase === "pick" || !sources.length);
  };

  const editable = phase === "map" || phase === "error";
  const busy = phase === "uploading" || phase === "ingesting" || phase === "done";
  const single = sources.length === 1;

  return (
    <div className="grid gap-5">
      {phase === "pick" || !sources.length ? (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn("flex flex-col items-center gap-3 rounded-lg border-2 border-dashed p-10 text-center", dragging && "border-primary bg-accent")}
        >
          <UploadCloud className="size-6 text-muted-foreground" />
          <p className="max-w-[52ch] text-muted-foreground">
            Drop a CSV, Parquet, zarr zip, netCDF or HDF5 file here. For a CSV file, this page shows a preview in your browser before the upload. You can select several files: they become one version.
          </p>
          <Button variant="outline" onClick={() => inputRef.current?.click()}>
            <FileUp /> Choose files
          </Button>
        </div>
      ) : null}
      <input
        ref={inputRef}
        type="file"
        multiple
        className="sr-only"
        accept={ACCEPT}
        onChange={(e) => {
          const files = [...(e.target.files ?? [])];
          e.target.value = "";
          if (files.length) void add(files, phase === "pick" || !sources.length);
        }}
      />

      {phase !== "pick"
        ? sources.map((s, i) => (
            <SourceCard
              key={s.key}
              datasetId={datasetId}
              source={s}
              index={i}
              single={single}
              editable={editable}
              expert={expert}
              problems={problemsBySource.get(s.key) ?? []}
              placementProblems={placementProblems.filter((p) => p.index === i).map((p) => p.message)}
              inspecting={startInspect.isPending || (progress?.index === i && phase === "map")}
              onPatch={(p) => patch(s.key, p)}
              onFormat={(f) => void setFormat(s, f)}
              onInspect={() => void inspect(s, i)}
              onRemove={() => {
                const rest = sources.filter((x) => x.key !== s.key);
                setSources(rest);
                if (!rest.length) setPhase("pick");
              }}
              onChooseAnother={() => {
                setSources([]);
                setPhase("pick");
              }}
            />
          ))
        : null}

      {sources.length && editable ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => inputRef.current?.click()}>
            <Plus /> Add a file
          </Button>
          <span className="text-xs text-muted-foreground">
            {single ? "Add files to make one version from several files (one file per year, a file per node of a tree…)." : `${sources.length} files make one version.`}
          </span>
        </div>
      ) : null}

      {sources.length > 1 && editable ? (
        <section aria-label="How the files combine" className="grid gap-2 rounded-lg border bg-muted/30 p-3">
          <h3 className="flex items-center gap-1.5 font-semibold">
            <Layers className="size-4" /> How the files combine
          </h3>
          <p className="text-xs text-muted-foreground">
            {expert
              ? "Files of one node combine by one rule: the only file, stacked along a dim (concatenate, one label per file), or merged (variables on shared coordinates)."
              : "Each file goes to a node of the tree (/ is the top). Two or more files in the same node must stack along a dim, for example one file per year, or merge, for example sales in one file and spend in another."}
          </p>
          <CombineSummary sources={sources} />
          {placementProblems.length ? (
            <ul aria-label="Problems with the files" className="grid gap-0.5 text-xs text-destructive">
              {placementProblems.map((p, i) => <li key={i}>{p.message}</li>)}
            </ul>
          ) : null}
        </section>
      ) : single && placementProblems.length ? (
        <ul aria-label="Problems with the files" className="grid gap-0.5 text-xs text-destructive">
          {placementProblems.map((p, i) => <li key={i}>{p.message}</li>)}
        </ul>
      ) : null}

      {sources.length && editable ? (
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Version message (optional)" htmlFor="version-msg" className="min-w-64 flex-1">
            <Input id="version-msg" value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Q3 refresh" />
          </Field>
          <Button onClick={() => void start()} disabled={blocking}>
            <UploadCloud /> Upload and ingest
          </Button>
        </div>
      ) : null}

      {phase === "uploading" ? (
        <div className="grid gap-2">
          <p className="text-[13px]">{single ? "The file uploads to quarantine storage…" : `File ${(progress?.index ?? 0) + 1} of ${sources.length} uploads to quarantine storage…`}</p>
          <Progress label="Upload progress" value={progress?.p ? progress.p.loaded / Math.max(1, progress.p.total) : null} />
        </div>
      ) : null}
      {phase === "ingesting" ? <p className="text-[13px]">{single ? "The upload is complete. The sandboxed ingest run starts…" : "The uploads are complete. The sandboxed ingest run starts and combines the files…"}</p> : null}
      {error && !busy ? <p role="alert" className="text-[13px] text-destructive">{error}</p> : null}
    </div>
  );
}

/** Node → files, as the version will hold them. */
function CombineSummary({ sources }: { sources: Source[] }) {
  const byNode = new Map<string, Source[]>();
  for (const s of sources) byNode.set(s.placement.node, [...(byNode.get(s.placement.node) ?? []), s]);
  return (
    <ul aria-label="Nodes of the version" className="grid gap-1 text-[13px]">
      {[...byNode.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([node, group]) => (
          <li key={node} className="flex flex-wrap items-baseline gap-1.5">
            <Mono className="font-semibold">{node}</Mono>
            <span className="text-muted-foreground">
              {group.length === 1 && group[0]!.placement.combine === "node"
                ? "←"
                : group[0]!.placement.combine === "concat"
                  ? `← stacked along ${group[0]!.placement.concatDim || "?"}:`
                  : `← merged (${group[0]!.placement.join}):`}
            </span>
            {group.map((s) => (
              <Badge key={s.key} variant="outline">
                {s.file.name}
                {s.placement.combine === "concat" ? ` = ${s.placement.concatLabel || "?"}` : ""}
              </Badge>
            ))}
          </li>
        ))}
    </ul>
  );
}

interface SourceCardProps {
  datasetId: string;
  source: Source;
  index: number;
  single: boolean;
  editable: boolean;
  expert: boolean;
  problems: string[];
  placementProblems: string[];
  inspecting: boolean;
  onPatch: (p: Partial<Source> | ((s: Source) => Partial<Source>)) => void;
  onFormat: (f: Format) => void;
  onInspect: () => void;
  onRemove: () => void;
  onChooseAnother: () => void;
}

function SourceCard({ datasetId, source: s, single, editable, expert, problems, placementProblems, inspecting, onPatch, onFormat, onInspect, onRemove, onChooseAnother }: SourceCardProps) {
  const name = s.file.name;
  const tabular = tabularFormat(s.format);
  const canInspect = s.format === "hdf5" || s.format === "netcdf" || s.format === "zarr_zip";
  const showPlacement = !single;
  return (
    <section aria-label={`File ${name}`} className={cn("grid gap-4", !single && "rounded-lg border p-3")}>
      <div className="flex flex-wrap items-center gap-3 rounded-lg border p-3">
        <FileUp className="size-4 text-muted-foreground" />
        <Mono>{name}</Mono>
        <span className="text-xs text-muted-foreground">{formatBytes(s.file.size)}</span>
        {editable ? (
          <NativeSelect aria-label={`Format of ${name}`} value={s.format ?? ""} onChange={(e) => e.target.value && onFormat(e.target.value as Format)} className="h-8 w-auto">
            {s.format ? null : <option value="">Select a format</option>}
            {FORMATS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </NativeSelect>
        ) : s.format ? (
          <Badge variant="outline">{s.format}</Badge>
        ) : null}
        {!s.format ? <Badge variant="danger">unsupported type</Badge> : null}
        {s.upload?.done ? <Badge variant="success">uploaded</Badge> : null}
        {editable ? (
          single ? (
            <Button variant="ghost" size="sm" className="ml-auto" onClick={onChooseAnother}>
              Choose another
            </Button>
          ) : (
            <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label={`Remove ${name}`} onClick={onRemove}>
              <Trash2 />
            </Button>
          )
        ) : null}
      </div>

      {showPlacement && editable ? <PlacementFields source={s} expert={expert} problems={placementProblems} onPatch={onPatch} /> : null}
      {single && editable ? (
        <details className="rounded-lg border px-3 py-2" open={s.placement.node !== "/" || s.placement.combine !== "node"}>
          <summary className="cursor-pointer text-[13px] font-medium">Put this file in a node of a tree (optional)</summary>
          <div className="pt-3">
            <PlacementFields source={s} expert={expert} problems={placementProblems} onPatch={onPatch} />
          </div>
        </details>
      ) : null}

      {s.preview && editable ? <CsvPreviewTable preview={s.preview} /> : null}

      {tabular && editable ? (
        <ColumnMappingTable
          format={s.format!}
          mappings={s.mappings}
          problems={problems}
          onMappings={(f) => onPatch((cur) => ({ mappings: f(cur.mappings) }))}
        />
      ) : null}

      {canInspect && editable ? (
        <InspectSection
          datasetId={datasetId}
          source={s}
          expert={expert}
          inspecting={inspecting}
          problems={s.format === "hdf5" && s.inspection ? problems : []}
          onInspect={onInspect}
          onPatch={onPatch}
        />
      ) : null}
    </section>
  );
}

function PlacementFields({ source: s, expert, problems, onPatch }: { source: Source; expert: boolean; problems: string[]; onPatch: SourceCardProps["onPatch"] }) {
  const name = s.file.name;
  const p = s.placement;
  const set = (patch: Partial<Source["placement"]>) => onPatch((cur) => ({ placement: { ...cur.placement, ...patch } }));
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Node" htmlFor={`node-${s.key}`} hint={expert ? undefined : "/ is the top of the tree. A file with groups puts them below this node."}>
          <Input id={`node-${s.key}`} aria-label={`Node for ${name}`} value={p.node} onChange={(e) => set({ node: e.target.value })} className="h-8 w-44 font-mono" aria-invalid={problems.some((m) => m.includes("node") || m.includes("Node"))} />
        </Field>
        <Field label="Combine with the other files of the node" htmlFor={`combine-${s.key}`}>
          <NativeSelect id={`combine-${s.key}`} aria-label={`Combine rule for ${name}`} value={p.combine} onChange={(e) => set({ combine: e.target.value as Placement["combine"] })} className="h-8 w-60">
            <option value="node">the only file of the node</option>
            <option value="concat">stack along a dim (concatenate)</option>
            <option value="merge">merge the variables</option>
          </NativeSelect>
        </Field>
        {p.combine === "concat" ? (
          <>
            <Field label="Dim to stack along" htmlFor={`cdim-${s.key}`}>
              <Input id={`cdim-${s.key}`} aria-label={`Stack dim for ${name}`} value={p.concatDim} onChange={(e) => set({ concatDim: e.target.value })} placeholder="year" className="h-8 w-28 font-mono" />
            </Field>
            <Field label="Label of this file" htmlFor={`clabel-${s.key}`}>
              <Input id={`clabel-${s.key}`} aria-label={`Label of ${name}`} value={p.concatLabel} onChange={(e) => set({ concatLabel: e.target.value })} placeholder="2024" className="h-8 w-28 font-mono" />
            </Field>
          </>
        ) : null}
        {p.combine !== "node" ? (
          <Field label="Align the coordinates" htmlFor={`join-${s.key}`}>
            <NativeSelect id={`join-${s.key}`} aria-label={`Coordinate alignment for ${name}`} value={p.join} onChange={(e) => set({ join: e.target.value as Placement["join"] })} className="h-8 w-56">
              <option value="exact">exact (the same labels)</option>
              <option value="inner">inner (the shared labels)</option>
              <option value="outer">outer (all labels, gaps are missing)</option>
            </NativeSelect>
          </Field>
        ) : null}
      </div>
      {problems.length ? (
        <ul className="grid gap-0.5 text-xs text-destructive">
          {problems.map((m, i) => <li key={i}>{m}</li>)}
        </ul>
      ) : null}
    </div>
  );
}

function InspectSection({
  datasetId,
  source: s,
  expert,
  inspecting,
  problems,
  onInspect,
  onPatch,
}: {
  datasetId: string;
  source: Source;
  expert: boolean;
  inspecting: boolean;
  problems: string[];
  onInspect: () => void;
  onPatch: SourceCardProps["onPatch"];
}) {
  const name = s.file.name;
  const raw = s.format === "hdf5";
  const inspection = useUploadInspection(datasetId, s.upload?.session.id ?? null, s.inspectRunId);
  const ready = inspection.data?.status === "ready" ? inspection.data.inspection : null;
  useEffect(() => {
    if (ready && s.inspection !== ready) {
      onPatch((cur) => ({ inspection: ready, hdf5Vars: cur.hdf5Vars.length ? cur.hdf5Vars : initialHdf5Vars(ready.arrays ?? []) }));
    }
  }, [ready, s.inspection, onPatch]);

  const selected = useMemo(() => new Set(s.hdf5Vars.filter((v) => v.include).map((v) => v.path)), [s.hdf5Vars]);
  const updateVars = (f: (vars: Hdf5VarDraft[]) => Hdf5VarDraft[]) =>
    onPatch((cur) => {
      const vars = f(cur.hdf5Vars);
      return { hdf5Vars: vars, hdf5Coords: coordDrafts(vars, cur.hdf5Coords) };
    });

  const polling = Boolean(s.inspectRunId) && (inspection.isPending || inspection.data?.status === "pending");
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">{raw ? "Arrays of the file" : "Structure of the file"}</h3>
        {!s.inspectRunId || inspection.isError ? (
          <Button size="sm" variant={raw ? "default" : "outline"} onClick={onInspect} disabled={inspecting}>
            {inspecting ? <Spinner /> : <Search />} {raw ? (s.upload?.done ? "List the arrays" : "Upload and list the arrays") : "Preview structure"}
          </Button>
        ) : null}
      </div>
      {!s.inspectRunId ? (
        <p className="text-xs text-muted-foreground">
          {raw
            ? "A raw HDF5 file has no dim names. The sandbox lists its arrays first (it reads only the metadata). Then you select the arrays to use and name their dims."
            : "Optional: the sandbox lists the groups and arrays of the file before the ingest. A file with groups becomes a tree of nodes."}
        </p>
      ) : null}
      {polling ? (
        <p role="status" className="flex items-center gap-2 text-[13px] text-muted-foreground">
          <Spinner /> The sandbox lists the arrays of {name}…
        </p>
      ) : null}
      {inspection.isError ? (
        <div role="alert" className="grid gap-1 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-[13px]">
          <span className="font-medium">The sandbox could not list the arrays of {name}.</span>
          <span className="break-words text-muted-foreground">{errorMessage(inspection.error)}</span>
        </div>
      ) : null}
      {ready ? (
        <ArrayBrowser
          inspection={ready}
          file={name}
          expert={expert}
          {...(raw
            ? {
                selected,
                onToggle: (path: string, on: boolean) => updateVars((vars) => vars.map((v) => (v.path === path ? { ...v, include: on } : v))),
              }
            : {})}
        />
      ) : null}
      {ready && raw ? (
        <Hdf5Mapper
          inspection={ready}
          vars={s.hdf5Vars}
          coords={s.hdf5Coords}
          expert={expert}
          problems={validateHdf5(s.hdf5Vars, s.hdf5Coords, ready.arrays ?? [])}
          onVar={(path, p) => updateVars((vars) => vars.map((v) => (v.path === path ? { ...v, ...p } : v)))}
          onCoord={(dim, node, p) => onPatch((cur) => ({ hdf5Coords: cur.hdf5Coords.map((c) => (c.dim === dim && c.node === node ? { ...c, ...p } : c)) }))}
        />
      ) : null}
      {problems.length ? (
        <ul aria-label={`Problems with the mapping of ${name}`} className="grid gap-0.5 text-xs text-destructive">
          {problems.map((m, i) => <li key={i}>{m}</li>)}
        </ul>
      ) : null}
    </div>
  );
}

function CsvPreviewTable({ preview }: { preview: CsvPreview }) {
  return (
    <div className="grid gap-2">
      <p className="text-xs text-muted-foreground">
        Local preview: first {preview.rows.length} rows{preview.truncated ? " (the file has more rows)" : ""}. Delimiter: <Mono>{preview.delimiter === "\t" ? "tab" : preview.delimiter}</Mono>.
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
  );
}

function ColumnMappingTable({
  format,
  mappings,
  problems,
  onMappings,
}: {
  format: Format;
  mappings: ColumnMapping[];
  problems: string[];
  onMappings: (f: (ms: ColumnMapping[]) => ColumnMapping[]) => void;
}) {
  const update = (i: number, patch: Partial<ColumnMapping>) => onMappings((ms) => ms.map((m, j) => (j === i ? { ...m, ...patch } : m)));
  return (
    <div className="grid gap-2">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Column mapping</h3>
        {format === "parquet" ? (
          <Button size="sm" variant="outline" onClick={() => onMappings((ms) => [...ms, { ...initialMappings(["column"], [])[0]!, column: "", name: "" }])}>
            <Plus /> Add column
          </Button>
        ) : null}
      </div>
      {format === "parquet" ? <p className="text-xs text-muted-foreground">Only the sandboxed ingest run can read Parquet files. Type the column names yourself.</p> : null}
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
                      {m.dimKind === "time" ? <Input aria-label="Frequency" placeholder="freq (D, W-SUN)" value={m.freq} onChange={(e) => update(i, { freq: e.target.value })} className="h-8 w-28" /> : null}
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
                    <Button variant="ghost" size="icon-sm" aria-label="Remove column" onClick={() => onMappings((ms) => ms.filter((_, j) => j !== i))}>
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
          {problems.map((p, i) => <li key={i}>{p}</li>)}
        </ul>
      ) : null}
    </div>
  );
}
