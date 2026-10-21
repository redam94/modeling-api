/**
 * "Show technical notes": the model's technical details of a readout (`snapshot.technical`),
 * behind a switch. Client pages use plain words; the analyst turns this on per dashboard
 * (`show_technical`) for readers who want the numbers behind the words.
 */
import { useId, useState } from "react";

import { minus } from "@/components/ods/format";
import { Switch } from "@/components/ui/overlays";
import { Label } from "@/components/ui/primitives";

import type { Readout } from "../types";

function valueText(v: string | number): string {
  return typeof v === "number" ? (Number.isInteger(v) ? String(v) : minus(v.toPrecision(4))) : v;
}

export function TechnicalToggle({ readout }: { readout: Pick<Readout, "model_ref" | "snapshot" | "seq"> }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const entries = Object.entries(readout.snapshot?.technical ?? {});
  return (
    <div className="grid gap-3 rounded-xl border border-dashed border-border-strong p-4">
      <div className="flex items-center gap-3">
        <Switch id={id} checked={open} onCheckedChange={setOpen} aria-controls={`${id}-notes`} />
        <Label htmlFor={id}>Show technical notes</Label>
      </div>
      {open ? (
        <dl id={`${id}-notes`} className="grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-6 gap-y-1 text-xs">
          <dt className="text-hint">Model</dt>
          <dd className="font-mono break-all text-foreground">{readout.model_ref}</dd>
          <dt className="text-hint">Readout</dt>
          <dd className="text-foreground tabular">{readout.seq}</dd>
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="font-mono break-all text-hint">{k}</dt>
              <dd className="font-mono break-all text-foreground tabular">{valueText(v)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
