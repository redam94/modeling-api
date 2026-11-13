/**
 * The builder's palette: pipeline steps (by kind), models (by namespace), datasets and plots,
 * searchable. Drag an item onto the spec, or select it and press "Add" (the keyboard path).
 *
 * v0.18 (D4, D17): a "From your team" group lists the library's model recipes and prep presets
 * (never ranked among the built-ins). Picking one and pressing "Add to the spec" asks the
 * server to compile it (`:use`, a prep preset's slots filled from the columns the pipeline
 * has at its end) and hands the result to the builder. "Add your own" sits at the foot.
 */
import { Plus, Search, Users } from "lucide-react";
import { useId, useMemo, useState } from "react";

import type { CatalogResponse, Dataset } from "@/api/types";
import { ErrorNotice, Spinner } from "@/components/common";
import { AddYourOwn } from "@/components/extend/add-your-own";
import { Button } from "@/components/ui/button";
import { Badge, Input, NativeSelect } from "@/components/ui/primitives";
import { latestModels, latestSteps, pinnedUse } from "@/features/editor/spec-hints";
import { teamItems } from "@/features/extend/team";
import { useLibraryItemUse, useLibraryItems, useLibraryRevision } from "@/features/library/api";
import { hasErrors, isPrepPresetDoc, KIND_LABEL, type Json, type LibraryItem, type LibraryUseResult, type Slot } from "@/features/library/types";
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

/** What the palette needs to put a team item into the spec (the visual builder supplies it). */
export interface TeamUse {
  /** Apply the compiled item (`spec_fragment` of a model recipe, `pipeline` of a prep preset). */
  onUse: (item: LibraryItem, result: LibraryUseResult) => void;
  /** Columns and dims at the end of the pipeline (a prep preset's slot choices). */
  names: { vars: string[]; dims: string[] };
  /** The spec's dataset (`dataset://…`), for the server's checks. */
  dataset: string | null;
}

export function SpecPalette({
  catalog,
  datasets,
  onAdd,
  disabled,
  team,
}: {
  catalog: CatalogResponse | null;
  datasets: Dataset[];
  onAdd: (item: PaletteItem) => void;
  disabled?: boolean | undefined;
  team?: TeamUse | undefined;
}) {
  const [q, setQ] = useState("");
  const [picked, setPickedRaw] = useState<string | null>(null);
  const [teamPick, setTeamPickRaw] = useState<string | null>(null);
  const setPicked = (key: string | null) => {
    setPickedRaw(key);
    setTeamPickRaw(null);
  };
  const setTeamPick = (id: string | null) => {
    setTeamPickRaw(id);
    setPickedRaw(null);
  };
  const teamList = useTeamPalette(Boolean(team));
  const teamItem = teamPick ? (teamList.data ?? []).find((i) => i.id === teamPick) : undefined;
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
            <summary className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium hover:bg-accent" title={g.hint}>
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
                        "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm hover:bg-accent",
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
        {team ? <TeamGroup q={q} picked={teamPick} onPick={(id) => setTeamPick(id)} /> : null}
        {groups.length === 0 ? <p className="px-2 text-sm text-muted-foreground">{q ? `No item matches “${q}”. Type a different word.` : "The app gets the catalog…"}</p> : null}
      </div>
      {team && teamItem ? (
        <TeamItemPanel key={teamItem.id} item={teamItem} team={team} disabled={disabled} />
      ) : current ? (
        <div className="grid gap-2 rounded-lg border bg-card p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
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
      {team ? <AddYourOwn kind="step" variant="compact" headingLevel={3} /> : null}
    </section>
  );
}

// ----------------------------------------------------------------------------- from your team

const TEAM_KINDS: ReadonlySet<LibraryItem["kind"]> = new Set(["model_recipe", "prep_preset"]);

/** The team's model recipes and prep presets (non-archived) that match `q`. */
export function teamPaletteItems(items: readonly LibraryItem[], q: string): LibraryItem[] {
  const needle = q.trim().toLowerCase();
  return teamItems(items)
    .filter((i) => TEAM_KINDS.has(i.kind))
    .filter((i) => !needle || [i.title, i.name, i.summary ?? "", ...(i.tags ?? [])].some((t) => t.toLowerCase().includes(needle)));
}

function useTeamPalette(enabled: boolean) {
  const items = useLibraryItems({}, { enabled });
  return items;
}

function TeamGroup({ q, picked, onPick }: { q: string; picked: string | null; onPick: (id: string) => void }) {
  const items = useTeamPalette(true);
  const list = useMemo(() => teamPaletteItems(items.data ?? [], q), [items.data, q]);
  if (!list.length) return null;
  return (
    <details open={Boolean(q) || picked !== null} className="rounded-md" role="listitem">
      <summary className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium hover:bg-accent" title="Model recipes and prep presets your team made in Make your own.">
        <Users aria-hidden className="size-3.5 text-highlight-ink" />
        From your team
        <span className="ml-auto text-xs text-muted-foreground">{list.length}</span>
      </summary>
      <ul className="grid grid-cols-[minmax(0,1fr)] gap-0.5 pb-1">
        {list.map((i) => (
          <li key={i.id}>
            <button
              type="button"
              onClick={() => onPick(i.id)}
              aria-pressed={picked === i.id}
              data-palette={`team:${i.id}`}
              title={i.summary || i.title}
              className={cn("flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm hover:bg-accent", picked === i.id && "bg-accent text-accent-foreground")}
            >
              <span className="min-w-0 flex-1 truncate">{i.title}</span>
              <span className="shrink-0 text-[10px] font-semibold text-muted-foreground">{i.kind === "model_recipe" ? "Model" : "Prep"}</span>
            </button>
          </li>
        ))}
      </ul>
    </details>
  );
}

/** A slot's value from the picker (a list for `var_list`). */
export function slotChoice(slot: Slot, value: string | string[] | undefined): Json | undefined {
  if (slot.kind === "var_list") return Array.isArray(value) && value.length ? value : undefined;
  return typeof value === "string" && value ? value : undefined;
}

function SlotField({ name, slot, names, value, onChange }: { name: string; slot: Slot; names: TeamUse["names"]; value: string | string[] | undefined; onChange: (v: string | string[]) => void }) {
  const id = useId();
  const options = slot.kind === "dim" ? names.dims : names.vars;
  if (slot.kind === "var_list") {
    const chosen = Array.isArray(value) ? value : [];
    return (
      <fieldset className="grid gap-1">
        <legend className="text-xs font-medium [overflow-wrap:anywhere]">{slot.label || name}</legend>
        <div className="grid max-h-28 gap-0.5 overflow-y-auto">
          {options.map((o) => (
            <label key={o} className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" checked={chosen.includes(o)} onChange={(e) => onChange(e.target.checked ? [...chosen, o] : chosen.filter((x) => x !== o))} />
              <span className="font-mono">{o}</span>
            </label>
          ))}
          {!options.length ? <span className="text-xs text-muted-foreground">Set the dataset first.</span> : null}
        </div>
      </fieldset>
    );
  }
  return (
    <div className="grid gap-1">
      <label htmlFor={id} className="text-xs font-medium [overflow-wrap:anywhere]">
        {slot.label || name}
      </label>
      <NativeSelect id={id} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)} className="h-8 text-xs">
        <option value="">{slot.kind === "dim" ? "Pick a dimension" : "Pick a column"}</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </NativeSelect>
    </div>
  );
}

function TeamItemPanel({ item, team, disabled }: { item: LibraryItem; team: TeamUse; disabled?: boolean | undefined }) {
  const revision = useLibraryRevision(item.kind === "prep_preset" ? item.id : undefined, item.latest_revision);
  const use = useLibraryItemUse(item.id);
  const [values, setValues] = useState<Record<string, string | string[]>>({});
  const [issues, setIssues] = useState<NonNullable<LibraryUseResult["issues"]>>([]);
  const doc = revision.data?.document;
  const slots = item.kind === "prep_preset" && isPrepPresetDoc(doc) ? Object.entries(doc.slots ?? {}) : [];
  const add = () => {
    setIssues([]);
    const filled: Record<string, Json> = {};
    for (const [name, slot] of slots) {
      const v = slotChoice(slot, values[name]);
      if (v !== undefined) filled[name] = v;
    }
    use.mutate(
      { slots: filled, exposed: {}, revision: item.latest_revision, ...(item.kind === "model_recipe" && team.dataset ? { dataset: team.dataset } : {}) },
      {
        onSuccess: (result) => {
          if (hasErrors(result.issues)) setIssues(result.issues ?? []);
          else team.onUse(item, result);
        },
      },
    );
  };
  return (
    <div className="grid gap-2.5 rounded-lg border bg-card p-3" data-testid="team-palette-item">
      <div className="grid gap-0.5">
        <span className="text-[10px] font-bold tracking-wide text-highlight-ink uppercase">From your team · {KIND_LABEL[item.kind]}</span>
        <span className="text-sm font-medium [overflow-wrap:anywhere]">{item.title}</span>
      </div>
      {item.summary ? <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{item.summary}</p> : null}
      {item.kind === "prep_preset" && revision.isPending ? <Spinner /> : null}
      {slots.map(([name, slot]) => (
        <SlotField key={name} name={name} slot={slot} names={team.names} value={values[name]} onChange={(v) => setValues({ ...values, [name]: v })} />
      ))}
      {item.kind === "model_recipe" ? <p className="text-xs text-muted-foreground">Adds its steps and sets the model block. The model you have now is replaced.</p> : null}
      {issues.length ? (
        <ul role="alert" className="grid gap-1 text-xs text-destructive">
          {issues
            .filter((i) => (i.severity ?? "error") === "error")
            .map((i, n) => (
              <li key={`${i.pointer ?? ""}-${n}`}>{i.message}</li>
            ))}
        </ul>
      ) : null}
      {use.error ? <ErrorNotice error={use.error} title="The item was not added" /> : null}
      <Button size="sm" className="justify-self-start" onClick={add} disabled={disabled || use.isPending || (item.kind === "prep_preset" && revision.isPending)}>
        {use.isPending ? <Spinner /> : <Plus />} Add to the spec
      </Button>
    </div>
  );
}
