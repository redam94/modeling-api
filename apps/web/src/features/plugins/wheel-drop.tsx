/**
 * The wheel drop zone of the upload page. One native button (Tab reaches it, Enter or Space
 * opens the file picker); dropping a file is an extra. Its own small component, not the shared
 * ODS `FileDrop`: sharing that module with the share-link readout would split it into a chunk of
 * its own and push the share link over its budget.
 */
import { Package } from "lucide-react";
import { useId, useRef, useState, type DragEvent } from "react";

import { cn } from "@/lib/utils";

export function WheelDrop({ onFile, label, hint, disabled = false }: { onFile: (file: File) => void; label: string; hint: string; disabled?: boolean }) {
  const id = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (!disabled && file) onFile(file);
  };
  return (
    <div className="grid gap-2">
      <button
        type="button"
        disabled={disabled}
        aria-labelledby={`${id}-label`}
        aria-describedby={`${id}-hint`}
        data-dragging={dragging || undefined}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "focus-ring flex w-full cursor-pointer flex-col items-center gap-2 rounded-2xl border-2 border-dashed border-border-strong bg-card px-4 py-8 text-center transition-colors",
          "hover:border-primary hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-border-strong disabled:hover:bg-card",
          dragging && "border-primary bg-accent",
        )}
      >
        <Package aria-hidden className="size-6 text-muted-foreground" />
        <span id={`${id}-label`} className="text-sm font-semibold [overflow-wrap:anywhere]">
          {label}
        </span>
        <span id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </span>
      </button>
      <input
        ref={inputRef}
        type="file"
        accept=".whl"
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        disabled={disabled}
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) onFile(file);
        }}
      />
    </div>
  );
}
