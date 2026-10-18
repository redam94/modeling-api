/**
 * The Insert panel: search everything a spec can reference (datasets, models, pipeline steps,
 * columns, plots, derived outputs) and put it into the spec. Datasets, models, steps and
 * outputs are structured edits (`spec-doc.ts`); a column name goes in at the cursor.
 */
import { Search } from "lucide-react";
import { forwardRef, useMemo, useState, type ReactNode } from "react";

import { Badge, Input } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { nodeKey, nodesWithVars } from "@/features/datasets/tree";

import { addStep, addToList, setDataset, setModel, setPath, starterWith } from "./spec-doc";
import {
  findModel,
  inputColumns,
  latestModels,
  latestSteps,
  namesForModel,
  namesOf,
  plotSnippets,
  pinnedUse,
  pluginId,
  type HintSources,
  type SpecShape,
} from "./spec-hints";

export interface InsertItem {
  key: string;
  title: string;
  id: string;
  detail?: string | null | undefined;
  /** Shown as a badge: already in the spec. */
  current?: boolean;
  /** Extra search text. */
  keywords?: string;
  apply: () => void;
}

interface Group {
  name: string;
  hint: string;
  items: InsertItem[];
}

export interface InsertPanelProps {
  text: string;
  shape: SpecShape;
  sources: HintSources;
  onText: (text: string) => void;
  onInsertAtCursor: (text: string) => void;
  readOnly?: boolean;
}

export const InsertPanel = forwardRef<HTMLInputElement, InsertPanelProps>(function InsertPanel(
  { text, shape, sources, onText, onInsertAtCursor, readOnly },
  searchRef,
) {
  const [q, setQ] = useState("");
  const groups = useMemo(
    () => buildGroups({ text, shape, sources, onText, onInsertAtCursor }),
    [text, shape, sources, onText, onInsertAtCursor],
  );
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return groups;
    return groups
      .map((g) => ({ ...g, items: g.items.filter((i) => [i.title, i.id, i.detail ?? "", i.keywords ?? ""].some((t) => t.toLowerCase().includes(needle))) }))
      .filter((g) => g.items.length);
  }, [groups, q]);

  return (
    <section aria-label="Insert into the spec" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
      <div className="relative">
        <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" aria-hidden />
        <Input
          ref={searchRef}
          aria-label="Search models, steps, datasets and columns"
          placeholder="Search models, steps, datasets, columns…"
          className="pl-8"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        Click an item to add it to the spec. In the editor, push <kbd className="font-mono">Ctrl Space</kbd> to see the same values at the cursor.
      </p>
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-1" role="list" aria-label="Insertable items">
        {filtered.map((g) => (
          <details key={g.name} open={Boolean(q) || g.name === "Pipeline steps" || g.name === "Columns"} className="rounded-md" role="listitem">
            <summary className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-accent" title={g.hint}>
              {g.name}
              <span className="ml-auto text-xs text-muted-foreground">{g.items.length}</span>
            </summary>
            <p className="px-2 pb-1 text-xs text-muted-foreground">{g.hint}</p>
            <ul className="grid grid-cols-[minmax(0,1fr)] gap-0.5 pb-1">
              {g.items.map((i) => (
                <li key={i.key}>
                  <ItemButton item={i} disabled={readOnly} />
                </li>
              ))}
            </ul>
          </details>
        ))}
        {filtered.length === 0 ? (
          <p className="px-2 text-[13px] text-muted-foreground">{q ? `No item matches “${q}”. Type a different word.` : "The app gets the catalog…"}</p>
        ) : null}
      </div>
    </section>
  );
});

function ItemButton({ item, disabled }: { item: InsertItem; disabled?: boolean | undefined }): ReactNode {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={item.apply}
      data-insert={item.key}
      title={item.detail ?? item.title}
      className={cn("grid w-full grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2 rounded-md px-2 py-1 text-left text-[13px] hover:bg-accent disabled:opacity-50", item.current && "bg-accent/50")}
    >
      <span className="min-w-0 truncate">
        {item.title}
        {item.current ? (
          <Badge variant="outline" className="ml-1.5 align-middle text-[10px]">
            in spec
          </Badge>
        ) : null}
      </span>
      <code className="shrink-0 font-mono text-[10px] text-muted-foreground">{item.id}</code>
      {item.detail ? <span className="col-span-2 truncate text-xs text-muted-foreground">{item.detail}</span> : null}
    </button>
  );
}

function applied(next: string | null, onText: (t: string) => void, title: string, detail?: string) {
  if (next === null) {
    toast.error("Correct the YAML first", "The app cannot read the YAML of the spec. Correct the YAML, then try again.");
    return;
  }
  onText(next);
  toast.success(title, detail);
}

export function buildGroups({
  text,
  shape,
  sources,
  onText,
  onInsertAtCursor,
}: Omit<InsertPanelProps, "readOnly">): Group[] {
  const { catalog } = sources;
  const groups: Group[] = [];
  const currentModel = shape.modelUse ? pluginId(shape.modelUse) : null;
  const model = shape.modelUse ? findModel(catalog, shape.modelUse) : null;

  groups.push({
    name: "Datasets",
    hint: "Sets data.dataset. The columns of the dataset then show under Columns.",
    items: sources.datasets.map((d) => ({
      key: `dataset:${d.ref}`,
      title: d.name,
      id: d.latest_version ? `v${d.latest_version}` : "empty",
      detail: d.description ?? d.about?.summary_generated,
      current: shape.datasetRef?.split("@")[0] === d.ref,
      keywords: d.ref,
      apply: () => applied(setDataset(text, `${d.ref}@latest`), onText, "Dataset set", d.ref),
    })),
  });

  groups.push({
    name: "Models",
    hint: "Sets model.use. If you change to a different model, the app removes the settings of the old model.",
    items: latestModels(catalog).map((m) => {
      const use = pinnedUse(m.id, m.version);
      return {
        key: `model:${m.id}`,
        title: m.about?.title ?? m.id,
        id: use,
        detail: m.about?.summary_plain ?? m.description,
        current: currentModel === m.id,
        keywords: [m.family ?? "", ...(m.about?.use_when ?? [])].join(" "),
        apply: () => applied(setModel(text, use), onText, "Model set", use),
      };
    }),
  });

  const names = namesForModel(shape, sources);
  const inPipeline = new Set(shape.steps.map((s) => pluginId(s.use)));
  groups.push({
    name: "Pipeline steps",
    hint: "Adds a step to the end of the pipeline. The app fills in the necessary settings for you to edit.",
    items: latestSteps(catalog).map((s) => {
      const use = pinnedUse(s.id, s.version);
      return {
        key: `step:${s.id}`,
        title: s.about?.title ?? s.summary ?? s.id,
        id: use,
        detail: s.summary_plain ?? s.summary,
        current: inPipeline.has(s.id),
        keywords: `${s.kind} ${s.about?.use_when?.join(" ") ?? ""}`,
        apply: () => applied(addStep(text, use, starterWith(s, names), s.id), onText, "Step added", use),
      };
    }),
  });

  // Dataset columns (insert the name the spec uses: the canonical one when bound), and the
  // names after the last pipeline stage when validation has computed them.
  const raw = namesOf(inputColumns(shape, sources));
  const boundTo = new Map(Object.entries({ ...shape.bind.dims, ...shape.bind.vars }).map(([c, ds]) => [ds, c]));
  const seen = new Set<string>();
  const columnItems: InsertItem[] = [...raw.dims.map((n) => ({ ...n, kind: "dim" })), ...raw.vars.map((n) => ({ ...n, kind: "var" }))].map((n) => {
    const canonical = boundTo.get(n.name);
    const name = canonical ?? n.name;
    seen.add(name);
    return {
      key: `col:${n.kind}:${n.name}`,
      title: canonical ? `${canonical} ← ${n.name}` : n.name,
      id: n.kind,
      detail: [n.detail, n.description].filter(Boolean).join(" · "),
      keywords: `${n.name} ${canonical ?? ""}`,
      apply: () => onInsertAtCursor(name),
    };
  });
  for (const [kind, list] of [["dim", names.dims], ["var", names.vars]] as const) {
    for (const n of list) {
      if (seen.has(n.name)) continue;
      columnItems.push({ key: `stage:${kind}:${n.name}`, title: n.name, id: `${kind} · derived`, detail: n.detail, apply: () => onInsertAtCursor(n.name) });
    }
  }
  groups.push({
    name: "Columns",
    hint: shape.datasetRef ? "Puts the name at the cursor. A bound name shows its canonical name." : "This spec has no dataset. Select a dataset to see its columns.",
    items: columnItems,
  });

  // M4: the nodes of a tree dataset (data.nodes picks the nodes the pipeline reads).
  if (sources.tree) {
    const tree = sources.tree;
    const selected = new Map(Object.entries(shape.nodes).map(([k, s]) => [s.path, k]));
    groups.push({
      name: "Tree nodes",
      hint: "This dataset is a tree. Adds the node to data.nodes: its variables go into the data the pipeline reads.",
      items: nodesWithVars(tree).map((p) => {
        const vars = Object.keys(tree[p]?.vars ?? {});
        const key = selected.get(p) ?? nodeKey(p, Object.keys(shape.nodes));
        return {
          key: `node:${p}`,
          title: p,
          id: key,
          detail: `${vars.length} ${vars.length === 1 ? "variable" : "variables"}: ${vars.join(", ")}`,
          current: selected.has(p),
          keywords: vars.join(" "),
          apply: () => applied(setPath(text, ["data", "nodes", key], { path: p }), onText, "Node added", `data.nodes.${key} reads ${p}`),
        };
      }),
    });
  }

  groups.push({
    name: "Plots",
    hint: "Adds the plot to outputs.plots. The plots of the model show first.",
    items: plotSnippets(catalog, model).map((p) => ({
      key: `plot:${p.body}`,
      title: p.description ?? p.label,
      id: p.body,
      apply: () => applied(addToList(text, ["outputs", "plots"], p.body), onText, "Plot added", p.body),
    })),
  });

  if (model?.derived?.length) {
    groups.push({
      name: "Derived outputs",
      hint: `Adds the output to outputs.derived. ${model.id} can calculate these quantities after the fit.`,
      items: model.derived.map((d) => ({
        key: `derived:${d.name}`,
        title: d.name,
        id: d.dims.length ? `[${d.dims.join(", ")}]` : "",
        detail: d.description,
        apply: () => applied(addToList(text, ["outputs", "derived"], d.name), onText, "Output added", d.name),
      })),
    });
  }
  return groups.filter((g) => g.items.length || g.name === "Columns");
}
