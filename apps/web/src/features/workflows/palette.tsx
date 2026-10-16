/**
 * The node palette: every implementation of `GET /v1/catalog/workflow-nodes`, grouped by kind
 * (each kind with its own About), searchable. Drag an item onto the canvas, or select it and
 * press "Add" (the keyboard path); its About card shows below the list.
 */
import { Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Badge, Input } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { cn } from "@/lib/utils";

import { useAbouts } from "./api";
import { PALETTE_MIME } from "./canvas-constants";
import type { Catalog } from "./ports";
import { KIND_COLOR, KIND_LABEL } from "./status";
import type { NodeImplementation, NodeKind } from "./types";
import { NODE_KINDS } from "./types";

export function NodePalette({ catalog, onAdd, disabled }: { catalog: Catalog; onAdd: (impl: string) => void; disabled?: boolean }) {
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const kinds = useAbouts("node_kind");
  const kindAbout = useMemo(() => new Map((kinds.data?.items ?? []).map((a) => [a.id.replace(/^kind\./, ""), a.about])), [kinds.data]);
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const match = (n: NodeImplementation) =>
      !needle || n.id.includes(needle) || n.about.title.toLowerCase().includes(needle) || n.about.summary_plain.toLowerCase().includes(needle);
    return NODE_KINDS.map((k) => [k, [...catalog.values()].filter((n) => n.kind === k && match(n))] as [NodeKind, NodeImplementation[]]).filter(([, list]) => list.length);
  }, [catalog, q]);
  const current = picked ? catalog.get(picked) : undefined;

  return (
    <section aria-label="Node palette" className="grid min-h-0 gap-3">
      <div className="relative">
        <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" aria-hidden />
        <Input aria-label="Search nodes" placeholder="Search nodes" className="pl-8" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="grid max-h-[46vh] gap-1 overflow-y-auto pr-1" role="list" aria-label="Nodes by kind">
        {groups.map(([kind, list]) => {
          const about = kindAbout.get(kind);
          return (
            <details key={kind} open={Boolean(q) || kind === "test" || kind === "model"} className="group rounded-md" role="listitem">
              <summary className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium hover:bg-accent" title={about?.summary_plain}>
                <span aria-hidden className="size-2 rounded-full" style={{ background: KIND_COLOR[kind] }} />
                {about?.title ?? KIND_LABEL[kind]}
                <span className="ml-auto text-xs text-muted-foreground">{list.length}</span>
              </summary>
              {about ? <p className="px-2 pb-1 text-xs text-muted-foreground">{about.summary_plain}</p> : null}
              <ul className="grid gap-0.5 pb-1">
                {list.map((n) => (
                  <li key={n.id}>
                    <button
                      type="button"
                      draggable={!disabled}
                      onDragStart={(e) => {
                        e.dataTransfer.setData(PALETTE_MIME, n.id);
                        e.dataTransfer.effectAllowed = "copy";
                      }}
                      onClick={() => setPicked(n.id)}
                      aria-pressed={picked === n.id}
                      data-node={n.id}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[13px] hover:bg-accent",
                        picked === n.id && "bg-accent text-accent-foreground",
                        !disabled && "cursor-grab active:cursor-grabbing",
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{n.about.title}</span>
                      <code className="shrink-0 font-mono text-[10px] text-muted-foreground">{n.id.split(".")[1]}</code>
                    </button>
                  </li>
                ))}
              </ul>
            </details>
          );
        })}
        {groups.length === 0 ? <p className="px-2 text-[13px] text-muted-foreground">No node matches “{q}”.</p> : null}
      </div>
      {current ? (
        <AboutCard
          about={current.about}
          headingLevel={4}
          heading={
            <span className="flex flex-wrap items-center gap-2">
              {current.about.title}
              <Badge variant="outline" className="font-mono">
                {current.id}
              </Badge>
            </span>
          }
          actions={
            <Button size="sm" onClick={() => onAdd(current.id)} disabled={disabled}>
              <Plus /> Add
            </Button>
          }
        />
      ) : (
        <p className="text-xs text-muted-foreground">Pick a node to read what it does, then drag it onto the canvas or press Add.</p>
      )}
    </section>
  );
}
