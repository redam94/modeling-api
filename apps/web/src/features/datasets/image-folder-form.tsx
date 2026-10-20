/**
 * The options of an image-folder upload (v0.14, `ImageFolderOptions`): a zip of PNG, JPEG or
 * TIFF images and a manifest table with one row for each image. The form names the manifest
 * and its columns (path, id, class label, split), the colour mode, an optional size for every
 * image and the maximum number of images. Only the sandboxed ingest run opens the zip, so the
 * column names are typed, not picked from the file.
 */
import { Field, Input, NativeSelect } from "@/components/ui/primitives";

import { DEFAULT_MAX_IMAGES, MAX_IMAGE_SIDE, MAX_IMAGES, validateImageFolder, type ImageFolderDraft, type ImageMode, type ImageResize } from "./mapping";
import { describedBy, indexesOf, ProblemList } from "./problem-list";

export interface ImageFolderFormProps {
  id: string;
  /** The file name (for the accessible labels). */
  name: string;
  draft: ImageFolderDraft;
  expert: boolean;
  onChange: (patch: Partial<ImageFolderDraft>) => void;
}

export function ImageFolderForm({ id, name, draft, expert, onChange }: ImageFolderFormProps) {
  const problems = validateImageFolder(draft);
  const listId = `${id}-images`;
  const aria = (field: keyof ImageFolderDraft, hint?: string) => {
    const idx = indexesOf(problems, (p) => p.field === field);
    return { "aria-invalid": idx.length > 0, "aria-describedby": describedBy(listId, idx, expert ? undefined : hint) };
  };
  const sized = Boolean(draft.height.trim() || draft.width.trim());
  const text = (field: keyof ImageFolderDraft, label: string, placeholder: string, hint: string, width = "w-40") => (
    <Field label={label} htmlFor={`${id}-${field}`} hint={expert ? undefined : hint}>
      <Input
        id={`${id}-${field}`}
        aria-label={`${label} for ${name}`}
        value={String(draft[field])}
        placeholder={placeholder}
        onChange={(e) => onChange({ [field]: e.target.value })}
        className={`h-8 ${width} font-mono`}
        {...aria(field, `${id}-${field}-hint`)}
      />
    </Field>
  );
  return (
    <section className="grid gap-3 rounded-lg border p-3" aria-label={`Image folder options for ${name}`} data-testid="image-folder-form">
      <div>
        <h3 className="font-semibold">Images and manifest</h3>
        {expert ? null : (
          <p className="text-xs text-muted-foreground">
            The zip holds the images (PNG, JPEG or TIFF, 8 bits for each colour) and a manifest table with one row for each image. The data gets the variable pixels(image, y, x, channel) with values from 0 to 255.
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        {text("manifest", "Manifest file", "manifest.csv", "The path of the table in the zip: .csv, .tsv or .parquet.", "w-48")}
        {text("pathColumn", "Path column", "path", "The column with the path of each image, relative to the top of the zip.")}
        {text("idColumn", "Id column (optional)", "id", "A column with a different name for each image. Empty: the row number.")}
        {text("labelColumn", "Class column (optional)", "label", "The class of each image. It becomes the target variable label.")}
        {text("splitColumn", "Split column (optional)", "split", "The train and test assignment, such as train or test. A column split can use it.")}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Colours" htmlFor={`${id}-mode`} hint={expert ? undefined : "Automatic: colour when an image has colour, else gray."}>
          <NativeSelect id={`${id}-mode`} aria-label={`Colours for ${name}`} value={draft.mode} onChange={(e) => onChange({ mode: e.target.value as ImageMode })} className="h-8 w-auto">
            <option value="auto">automatic</option>
            <option value="gray">gray</option>
            <option value="rgb">colour (RGB)</option>
          </NativeSelect>
        </Field>
        {text("height", "Height (pixels)", "keep", `Resize every image to this height (1 to ${MAX_IMAGE_SIDE}). Empty: all images must have the same size.`, "w-28")}
        {text("width", "Width (pixels)", "keep", `Resize every image to this width (1 to ${MAX_IMAGE_SIDE}).`, "w-28")}
        {sized ? (
          <Field label="Resize" htmlFor={`${id}-resize`} hint={expert ? undefined : "Stretch changes the shape. Crop cuts the sides. Pad adds black borders."}>
            <NativeSelect id={`${id}-resize`} aria-label={`Resize for ${name}`} value={draft.resize} onChange={(e) => onChange({ resize: e.target.value as ImageResize })} className="h-8 w-auto">
              <option value="stretch">stretch</option>
              <option value="crop">crop to the shape, then resize</option>
              <option value="pad">resize, then pad</option>
            </NativeSelect>
          </Field>
        ) : null}
        {text("maxImages", "Maximum number of images", String(DEFAULT_MAX_IMAGES), `The ingest stops when the manifest has more rows (1 to ${MAX_IMAGES.toLocaleString("en-US")}).`, "w-32")}
      </div>
      <ProblemList id={listId} label={`Problems with the image options of ${name}`} messages={problems.map((p) => p.message)} />
    </section>
  );
}
