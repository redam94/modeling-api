/**
 * The builder's palette: pipeline steps (by kind), models (by namespace), datasets and plots,
 * searchable. Drag an item onto the spec, or select it and press "Add" (the keyboard path).
 */
import { Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";

import type { CatalogResponse, Dataset } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Badge, Input } from "@/components/ui/primitives";
import { latestModels, latestSteps, pinnedUse } from "@/features/editor/spec-hints";
import { cn } from "@/lib/utils";

import { dragPayload, type PaletteItem, type PaletteKind } from "./dnd";

const STEP_KIND_LABEL: Record<string, string> = {
  reshape: "Reshape steps",
  value: "Value steps",
  feature: "Feature steps",
};

interface Group {
  key: string;
  title: string;
  hint: string;
  items: (PaletteItem & { title: string; detail: string | null; keywords: string })[];
}

function groupsOf(catalog: CatalogResponse | null, datasets: Dataset[]): Group[] {
  const groups: Group[] = [];
  groups.push({
    key: "dataset",
    title: "Datasets",
    hint: "Drag a dataset to the Data block.",
    items: datasets.map((d) => ({
      kind: "dataset",
      id: `${d.ref}@latest`,
      title: d.name,
      detail: d.description ?? d.about?.summary_generated ?? null,
      keywords: d.ref,
    })),
  });
  const steps = latestSteps(catalog);
  for (const kind of ["reshape", "value", "feature"]) {
    groups.push({
      key: `step:${kind}`,
      title: STEP_KIND_LABEL[kind] ?? kind,
      hint: "Drag a step to a position between steps in the pipeline.",
      items: steps
        .filter((s) => s.kind === kind)
        .map((s) => ({
          kind: "step",
          id: pinnedUse(s.id, s.version),
          title: s.about?.title ?? s.summary ?? s.id,
          detail: s.summary_plain ?? s.summary ?? null,
          keywords: `${s.id} ${s.about?.use_when?.join(" ") ?? ""}`,
        })),
    });
  }
  const models = latestModels(catalog);
  const namespaces = [...new Set(models.map((m) => m.id.split(".")[0] ?? m.id))];
  for (const ns of namespaces) {
    groups.push({
      key: `model:${ns}`,
      title: `Models · ${ns}`,
      hint: "Drag a model to the Model block.",
      items: models
        .filter((m) => m.id.split(".")[0] === ns)
        .map((m) => ({
          kind: "model",
          id: pinnedUse(m.id, m.version),
          title: m.about?.title ?? m.id,
          detail: m.about?.summary_plain ?? m.description ?? null,
          keywords: `${m.id} ${m.family ?? ""} ${m.about?.use_when?.join(" ") ?? ""}`,
        })),
    });
  }
  groups.push({
    key: "plot",
    title: "Plots",
    hint: "Drag a plot to the Outputs block.",
    items: (catalog?.plots ?? [])
      .filter((p) => !p.yanked)
      .map((p) => ({ kind: "plot", id: p.id, title: p.title, detail: p.about?.summary_plain ?? null, keywords: p.id })),
  });
  return groups.filter((g) => g.items.length);
}

/** Theme series colours, as the workflow palette uses for node kinds. */
export const KIND_COLOR: Record<PaletteKind, string> = {
  dataset: "var(--series-3)",
  step: "var(--series-5)",
  model: "var(--series-1)",
  plot: "var(--series-2)",
};

export function SpecPalette({
  catalog,
  datasets,
  onAdd,
  disabled,
}: {
  catalog: CatalogResponse | null;
  datasets: Dataset[];
  onAdd: (item: PaletteItem) => void;
  disabled?: boolean | undefined;
}) {
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const all = groupsOf(catalog, datasets);
    if (!needle) return all;
    return all
      .map((g) => ({ ...g, items: g.items.filter((i) => [i.title, i.id, i.detail ?? "", i.keywords].some((t) => t.toLowerCase().includes(needle))) }))
      .filter((g) => g.items.length);
  }, [catalog, datasets, q]);
  const current = picked ? groups.flatMap((g) => g.items).find((i) => `${i.kind}:${i.id}` === picked) : undefined;

  return (
    <section aria-label="Spec palette" className="grid min-h-0 min-w-0 grid-cols-[minmax(0,1fr)] content-start gap-3">
      <div className="relative">
        <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" aria-hidden />
        <Input aria-label="Search the palette" placeholder="Search steps, models…" className="pl-8" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="grid max-h-[calc(100svh-24rem)] min-w-0 grid-cols-[minmax(0,1fr)] gap-1 overflow-y-auto pr-1" role="list" aria-label="Palette items">
        {groups.map((g) => (
          <details key={g.key} open={Boolean(q) || g.key.startsWith("step:")} className="rounded-md" role="listitem">
            <summary className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-accent" title={g.hint}>
              <span aria-hidden className="size-2 rounded-full" style={{ background: KIND_COLOR[g.items[0]!.kind] }} />
              {g.title}
              <span className="ml-auto text-xs text-muted-foreground">{g.items.length}</span>
            </summary>
            <ul className="grid grid-cols-[minmax(0,1fr)] gap-0.5 pb-1">
              {g.items.map((i) => {
                const key = `${i.kind}:${i.id}`;
                return (
                  <li key={key}>
                    <button
                      type="button"
                      draggable={!disabled}
                      onDragStart={(e) => {
                        const [mime, data] = dragPayload(i);
                        e.dataTransfer.setData(mime, data);
                        e.dataTransfer.setData("text/plain", i.id);
                        e.dataTransfer.effectAllowed = "copy";
                      }}
                      onClick={() => setPicked(key)}
                      aria-pressed={picked === key}
                      data-palette={key}
                      title={i.detail ?? i.title}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[13px] hover:bg-accent",
                        picked === key && "bg-accent text-accent-foreground",
                        !disabled && "cursor-grab active:cursor-grabbing",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{i.title}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </details>
        ))}
        {groups.length === 0 ? <p className="px-2 text-[13px] text-muted-foreground">{q ? `No item matches “${q}”. Type a different word.` : "The app gets the catalog…"}</p> : null}
      </div>
      {current ? (
        <div className="grid gap-2 rounded-lg border bg-card p-3">
          <div className="flex flex-wrap items-center gap-2 text-[13px] font-medium">
            {current.title}
            <Badge variant="outline" className="font-mono text-[10px]">
              {current.id}
            </Badge>
          </div>
          {current.detail ? <p className="text-xs text-muted-foreground">{current.detail}</p> : null}
          <Button size="sm" className="justify-self-start" onClick={() => onAdd(current)} disabled={disabled}>
            <Plus /> Add
          </Button>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">Drag an item to the spec. Or select the item and click Add.</p>
      )}
    </section>
  );
}
