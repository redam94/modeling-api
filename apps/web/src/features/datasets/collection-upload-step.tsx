/**
 * The upload wizard step "Folder of images" (v0.15): a folder of PNG, JPEG or TIFF images
 * becomes an image collection version (`layout: "collection"`). The browser hashes every image,
 * asks the server which images the latest collection version of the dataset does not have,
 * and uploads only those, in zip parts (`collection-upload.ts`). A manifest file is optional:
 * without one, the first folder under the selected folder is the class of an image.
 *
 * The upload can resume: after a reload, the user selects the same folder again, and the parts
 * that arrived are not sent again.
 */
import { useQueryClient } from "@tanstack/react-query";
import { FolderUp, ImageIcon, RotateCcw, UploadCloud } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { errorMessage } from "@/api/client";
import { qk, useProjectScope } from "@/api/queries";
import type { DatasetVersion } from "@/api/types";
import { Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Field, Input, NativeSelect, Progress } from "@/components/ui/primitives";
import { useEffectivePersona } from "@/features/guided/persona";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

import {
  clearResume,
  collectionKey,
  collectionOptions,
  createCollectionVersion,
  DEFAULT_COLLECTION_DRAFT,
  draftSize,
  httpCollectionApi,
  imageExtension,
  MAX_COLLECTION_SIDE,
  planCollection,
  uploadCollection,
  validateCollectionDraft,
  type CollectionDraft,
  type CollectionPlan,
  type DraftProblem,
  type ImageMode,
  type ImageResize,
  type UploadPhase,
} from "./collection-upload";
import { describedBy, indexesOf, ProblemList } from "./problem-list";

type Step = "pick" | "hashing" | "uploading" | "creating" | "done" | "error";

interface Status {
  step: Step;
  /** Hashing: images done / all. */
  hashed?: [number, number];
  phase?: UploadPhase;
  /** Parts skipped because they arrived before (resume). */
  skipped: number;
}

const ID = "collection";

/** A sentence for the progress of the upload. */
function statusText(s: Status, plan: CollectionPlan | null): string {
  if (s.step === "hashing") return s.hashed ? `The browser calculates the checksums of the images: ${s.hashed[0]} of ${s.hashed[1]}.` : "The browser reads the folder…";
  if (s.step === "creating") return "The upload is complete. The sandboxed ingest run starts…";
  const p = s.phase;
  if (!p) return "";
  if (p.phase === "checking") return `The server compares the images with the latest version of the dataset: ${p.done} of ${p.total}.`;
  if (p.phase === "manifest") return `The manifest of ${plan?.images.length ?? 0} images uploads…`;
  if (p.phase === "skipped") return `Part ${p.index + 1} of ${p.count} is already in storage. The app does not upload it again.`;
  return `Part ${p.index + 1} of ${p.count} uploads: ${formatBytes(p.loaded)} of ${formatBytes(p.total)}${p.attempt > 1 ? ` (try ${p.attempt} of 3)` : ""}.`;
}

function progressValue(s: Status): number | null {
  if (s.step === "hashing" && s.hashed) return s.hashed[0] / Math.max(1, s.hashed[1]);
  const p = s.phase;
  if (s.step !== "uploading" || !p) return null;
  if (p.phase === "checking") return p.done / Math.max(1, p.total);
  if (p.phase === "part") return (p.index + p.loaded / Math.max(1, p.total)) / Math.max(1, p.count);
  if (p.phase === "skipped") return (p.index + 1) / Math.max(1, p.count);
  return null;
}

export interface CollectionUploadProps {
  datasetId: string;
  onDone: (v: DatasetVersion) => void;
  onCancel: () => void;
}

export function CollectionUpload({ datasetId, onDone, onCancel }: CollectionUploadProps) {
  const scope = useProjectScope();
  const qc = useQueryClient();
  const expert = useEffectivePersona() === "expert";
  const folderRef = useRef<HTMLInputElement>(null);
  const manifestRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [manifest, setManifest] = useState<File | null>(null);
  const [draft, setDraft] = useState<CollectionDraft>(DEFAULT_COLLECTION_DRAFT);
  const [message, setMessage] = useState("");
  const [status, setStatus] = useState<Status>({ step: "pick", skipped: 0 });
  const [plan, setPlan] = useState<CollectionPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [planProblems, setPlanProblems] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  // A folder input: React has no prop for `webkitdirectory`.
  useEffect(() => {
    folderRef.current?.setAttribute("webkitdirectory", "");
    folderRef.current?.setAttribute("directory", "");
  }, []);
  useEffect(() => () => abortRef.current?.abort(), []);

  const images = useMemo(() => files.filter((f) => imageExtension(f.name) !== null), [files]);
  const folderName = files[0]?.webkitRelativePath?.split("/")[0] || null;
  const problems = validateCollectionDraft(draft, manifest !== null);
  const busy = status.step === "hashing" || status.step === "uploading" || status.step === "creating" || status.step === "done";
  const blocking = !images.length || problems.length > 0;
  const set = (patch: Partial<CollectionDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setPlanProblems([]);
  };
  const listId = `${ID}-problems`;
  const aria = (field: DraftProblem["field"], hint?: string) => {
    const idx = indexesOf(problems, (p) => p.field === field);
    return { "aria-invalid": idx.length > 0, "aria-describedby": describedBy(listId, idx, expert ? undefined : hint) };
  };

  const start = async () => {
    if (blocking || busy) return;
    setError(null);
    setPlanProblems([]);
    const abort = new AbortController();
    abortRef.current = abort;
    try {
      setStatus({ step: "hashing", skipped: 0 });
      const text = manifest ? await manifest.text() : null;
      const planned = await planCollection(files, {
        labelFromFolder: draft.labelFrom === "folder",
        manifest: manifest && text !== null ? { name: manifest.name, text } : null,
        pathColumn: draft.pathColumn,
        labelColumn: draft.labelFrom === "column" ? draft.labelColumn : "",
        splitColumn: draft.splitColumn,
        onProgress: (done, total) => setStatus((s) => ({ ...s, hashed: [done, total] })),
        signal: abort.signal,
      });
      setPlan(planned);
      if (planned.problems.length) {
        setPlanProblems(planned.problems);
        setStatus({ step: "pick", skipped: 0 });
        return;
      }
      setStatus({ step: "uploading", skipped: 0 });
      const options = collectionOptions(planned, { idColumn: draft.idColumn, mode: draft.mode, size: draftSize(draft), resize: draft.resize });
      const result = await uploadCollection(httpCollectionApi(datasetId, scope.headers), {
        datasetId,
        plan: planned,
        options,
        baseVersion: draft.base === "none" ? 0 : null,
        onPhase: (phase) => setStatus((s) => ({ ...s, phase, skipped: s.skipped + (phase.phase === "skipped" ? 1 : 0) })),
        signal: abort.signal,
      });
      setStatus((s) => ({ ...s, step: "creating" }));
      const version = await createCollectionVersion(
        datasetId,
        { collection: result.source, ...(message.trim() ? { message: message.trim() } : {}) },
        // stable per uploads: "Try again" after a lost answer gets the version, not a 409
        await collectionKey(datasetId, result.source),
        scope.headers,
      );
      clearResume(datasetId, result.manifestSha256);
      await qc.invalidateQueries({ queryKey: qk.dataset(scope.key, datasetId) });
      setStatus((s) => ({ ...s, step: "done" }));
      onDone(version);
    } catch (e) {
      if (abort.signal.aborted) return;
      setError(errorMessage(e));
      setStatus((s) => ({ ...s, step: "error" }));
    }
  };

  const sized = Boolean(draft.height.trim() || draft.width.trim());
  const text = (field: "pathColumn" | "labelColumn" | "splitColumn" | "idColumn" | "height" | "width", label: string, placeholder: string, hint: string, width = "w-36") => (
    <Field label={label} htmlFor={`${ID}-${field}`} hint={expert ? undefined : hint}>
      <Input
        id={`${ID}-${field}`}
        value={draft[field]}
        placeholder={placeholder}
        disabled={busy}
        onChange={(e) => set({ [field]: e.target.value })}
        className={`h-8 ${width} font-mono`}
        {...aria(field, `${ID}-${field}-hint`)}
      />
    </Field>
  );

  return (
    <section aria-label="Folder of images" className="grid grid-cols-[minmax(0,1fr)] gap-4" data-testid="collection-upload">
      <div className="grid gap-1">
        <h3 className="flex items-center gap-1.5 font-semibold">
          <ImageIcon className="size-4" /> Folder of images
        </h3>
        {expert ? null : (
          <p className="max-w-[72ch] text-xs text-muted-foreground">
            Select a folder of PNG, JPEG or TIFF images. The version becomes an image collection. Put the images of each class in a folder with the name of the class, or select a manifest file. The browser uploads only the images that the latest version does not have.
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-lg border p-3">
        <Button variant="outline" size="sm" disabled={busy} onClick={() => folderRef.current?.click()}>
          <FolderUp /> {files.length ? "Choose another folder" : "Choose a folder"}
        </Button>
        {files.length ? (
          <span className="text-[13px]" data-testid="collection-folder-summary">
            {folderName ? <Mono>{folderName}</Mono> : null} {images.length.toLocaleString("en-US")} images
            {files.length > images.length ? `, ${files.length - images.length} other files (not uploaded)` : ""}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">No folder selected.</span>
        )}
        <span className="basis-full" />
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => manifestRef.current?.click()}>
          {manifest ? "Choose another manifest" : "Add a manifest file (optional)"}
        </Button>
        {manifest ? (
          <>
            <Badge variant="outline">{manifest.name}</Badge>
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => setManifest(null)}>
              Remove the manifest
            </Button>
          </>
        ) : null}
        <input
          ref={folderRef}
          type="file"
          multiple
          className="sr-only"
          aria-label="Folder of images"
          data-testid="collection-folder-input"
          onChange={(e) => {
            const picked = [...(e.target.files ?? [])];
            e.target.value = "";
            if (picked.length) {
              setFiles(picked);
              setPlan(null);
              setPlanProblems([]);
              setStatus({ step: "pick", skipped: 0 });
            }
          }}
        />
        <input
          ref={manifestRef}
          type="file"
          className="sr-only"
          accept=".csv,.tsv,.txt"
          aria-label="Manifest file"
          data-testid="collection-manifest-input"
          onChange={(e) => {
            const picked = e.target.files?.[0] ?? null;
            e.target.value = "";
            if (picked) {
              setManifest(picked);
              setDraft((d) => ({ ...d, labelFrom: d.labelFrom === "folder" ? "column" : d.labelFrom }));
              setPlanProblems([]);
            }
          }}
        />
      </div>

      <section aria-label="Options of the image collection" className="grid gap-3 rounded-lg border p-3">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Class of each image" htmlFor={`${ID}-labelFrom`} hint={expert ? undefined : "The class becomes the target variable label."}>
            <NativeSelect id={`${ID}-labelFrom`} value={draft.labelFrom} disabled={busy} onChange={(e) => set({ labelFrom: e.target.value as CollectionDraft["labelFrom"] })} className="h-8 w-auto" {...aria("labelFrom")}>
              <option value="folder">the name of the first folder</option>
              <option value="column">a column of the manifest</option>
              <option value="none">no class</option>
            </NativeSelect>
          </Field>
          {draft.labelFrom === "column" ? text("labelColumn", "Class column", "label", "The column of the manifest with the class of each image.") : null}
          {manifest ? text("pathColumn", "Path column", "path", "The column with the path of each image under the folder, for example circle/a.png.") : null}
          {manifest ? text("splitColumn", "Split column (optional)", "split", "The train and test assignment, such as train or test.") : null}
          {manifest ? text("idColumn", "Id column (optional)", "id", "A column with a different name for each image. Empty: the path.") : null}
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Colours" htmlFor={`${ID}-mode`} hint={expert ? undefined : "Automatic: colour when an image has colour, else gray."}>
            <NativeSelect id={`${ID}-mode`} value={draft.mode} disabled={busy} onChange={(e) => set({ mode: e.target.value as ImageMode })} className="h-8 w-auto">
              <option value="auto">automatic</option>
              <option value="gray">gray</option>
              <option value="rgb">colour (RGB)</option>
            </NativeSelect>
          </Field>
          {text("height", "Height (pixels)", "keep", `The size that models read the images at (1 to ${MAX_COLLECTION_SIDE}). Empty: the size of the images.`, "w-28")}
          {text("width", "Width (pixels)", "keep", `The width that models read the images at (1 to ${MAX_COLLECTION_SIDE}).`, "w-28")}
          {sized ? (
            <Field label="Resize" htmlFor={`${ID}-resize`} hint={expert ? undefined : "Stretch changes the shape. Crop cuts the sides. Pad adds black borders."}>
              <NativeSelect id={`${ID}-resize`} value={draft.resize} disabled={busy} onChange={(e) => set({ resize: e.target.value as ImageResize })} className="h-8 w-auto">
                <option value="stretch">stretch</option>
                <option value="crop">crop to the shape, then resize</option>
                <option value="pad">resize, then pad</option>
              </NativeSelect>
            </Field>
          ) : null}
          <Field label="Images to upload" htmlFor={`${ID}-base`} hint={expert ? undefined : "Only new images: the server keeps the images of the latest version, and the browser does not upload them again."}>
            <NativeSelect id={`${ID}-base`} value={draft.base} disabled={busy} onChange={(e) => set({ base: e.target.value as CollectionDraft["base"] })} className="h-8 w-auto">
              <option value="latest">only images that are not in the latest version</option>
              <option value="none">all images (no base version)</option>
            </NativeSelect>
          </Field>
        </div>
        <ProblemList id={listId} label="Problems with the image collection" messages={[...problems.map((p) => p.message), ...planProblems]} />
      </section>

      <div className="flex flex-wrap items-end gap-3">
        <Field label="Version message (optional)" htmlFor={`${ID}-msg`} className="min-w-64 flex-1">
          <Input id={`${ID}-msg`} value={message} disabled={busy} onChange={(e) => setMessage(e.target.value)} placeholder="New photos of May" />
        </Field>
        {status.step === "error" ? (
          <Button onClick={() => void start()} disabled={blocking}>
            <RotateCcw /> Try again
          </Button>
        ) : (
          <Button onClick={() => void start()} disabled={blocking || busy} aria-describedby={`${ID}-blocked`}>
            <UploadCloud /> Upload images
          </Button>
        )}
        <Button variant="ghost" disabled={status.step === "creating" || status.step === "done"} onClick={() => {
          abortRef.current?.abort();
          onCancel();
        }}>
          {busy ? "Stop" : "Upload files instead"}
        </Button>
        <p id={`${ID}-blocked`} role="status" className={cn("basis-full text-xs text-muted-foreground", !blocking && "sr-only")}>
          {!images.length ? "Choose a folder with images to upload." : problems.length ? `Correct ${problems.length === 1 ? "the problem" : `the ${problems.length} problems`} above to upload.` : ""}
        </p>
      </div>

      {busy ? (
        <div className="grid gap-2" data-testid="collection-progress">
          <p role="status" className="text-[13px]">{statusText(status, plan)}</p>
          <Progress label="Upload progress of the images" value={progressValue(status)} />
          {plan && status.step !== "hashing" ? (
            <p className="text-xs text-muted-foreground">
              {plan.images.length.toLocaleString("en-US")} images ({plan.distinct.toLocaleString("en-US")} different, {formatBytes(plan.totalBytes)}).
              {status.skipped ? ` ${status.skipped} parts were already in storage.` : ""}
            </p>
          ) : null}
        </div>
      ) : null}
      {error && status.step === "error" ? (
        <p role="alert" className="text-[13px] text-destructive">
          {error} Click Try again. The parts that arrived are not uploaded again.
        </p>
      ) : null}
    </section>
  );
}
