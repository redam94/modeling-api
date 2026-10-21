/**
 * FileDrop: drop a file here, or choose one. The whole zone is one native button, so Tab reaches
 * it and Enter or Space opens the file picker; drag and drop is an extra, never the only way.
 * Files that do not match `accept` are left out, and the zone says which in words.
 */
import { FileUp } from "lucide-react";
import { useId, useRef, useState, type DragEvent } from "react";

import { cn } from "@/lib/utils";

import { InlineMessage } from "./inline-message";

/** True when the file matches an `accept` list (".csv,text/csv,image/*"); an empty list accepts all. */
export function acceptsFile(file: Pick<File, "name" | "type">, accept: string | undefined): boolean {
  const rules = (accept ?? "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  if (!rules.length) return true;
  const name = file.name.toLowerCase();
  const type = (file.type || "").toLowerCase();
  return rules.some((r) => {
    if (r.startsWith(".")) return name.endsWith(r);
    if (r.endsWith("/*")) return type.startsWith(r.slice(0, -1));
    return type === r;
  });
}

export function FileDrop({
  onFiles,
  label = "Drop a file here, or choose a file",
  hint,
  accept,
  multiple = false,
  disabled = false,
  className,
}: {
  onFiles: (files: File[]) => void;
  label?: string;
  /** One line under the label ("A .csv file with one row for each week. Up to 520 rows."). */
  hint?: string;
  accept?: string;
  multiple?: boolean;
  disabled?: boolean;
  className?: string;
}) {
  const id = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [rejected, setRejected] = useState<string[]>([]);

  const take = (list: File[]) => {
    const ok = list.filter((f) => acceptsFile(f, accept));
    const picked = multiple ? ok : ok.slice(0, 1);
    setRejected(list.filter((f) => !acceptsFile(f, accept)).map((f) => f.name));
    if (picked.length) onFiles(picked);
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    take([...e.dataTransfer.files]);
  };

  return (
    <div data-slot="file-drop" className={cn("grid gap-2", className)}>
      <button
        type="button"
        disabled={disabled}
        aria-labelledby={`${id}-label`}
        aria-describedby={hint ? `${id}-hint` : undefined}
        data-dragging={dragging || undefined}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "focus-ring flex w-full cursor-pointer flex-col items-center gap-2 rounded-lg border-2 border-dashed border-border-strong bg-card px-4 py-8 text-center transition-colors",
          "hover:border-primary hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-border-strong disabled:hover:bg-card",
          dragging && "border-primary bg-accent",
        )}
      >
        <FileUp aria-hidden className="size-6 text-primary" />
        <span id={`${id}-label`} className="text-sm font-semibold text-foreground">
          {label}
        </span>
        {hint ? (
          <span id={`${id}-hint`} className="max-w-[52ch] text-xs text-hint">
            {hint}
          </span>
        ) : null}
      </button>
      <input
        ref={inputRef}
        type="file"
        tabIndex={-1}
        aria-hidden
        className="sr-only"
        accept={accept}
        multiple={multiple}
        disabled={disabled}
        onChange={(e) => {
          const files = [...(e.target.files ?? [])];
          e.target.value = "";
          if (files.length) take(files);
        }}
      />
      {rejected.length ? (
        <InlineMessage tone="error" size="s">
          {rejected.length === 1 ? `You cannot use the file ${rejected[0]}.` : `You cannot use these files: ${rejected.join(", ")}.`}
          {accept ? ` Use a file of this type: ${accept.split(",").map((s) => s.trim()).join(", ")}.` : ""}
        </InlineMessage>
      ) : null}
    </div>
  );
}
