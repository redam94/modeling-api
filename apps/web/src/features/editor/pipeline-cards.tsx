import { ChevronDown, History, Scissors } from "lucide-react";
import { useState } from "react";

import type { DataSchema, Issue, StageSchema } from "@/api/types";
import { Mono } from "@/components/common";
import { Badge } from "@/components/ui/primitives";
import { AboutButton, AboutInline } from "@/features/about/about-card";
import { cn } from "@/lib/utils";

import type { PipelineModel, StepCard } from "./pipeline";

const KIND_VARIANT = { reshape: "info", value: "secondary", feature: "outline" } as const;

function SchemaSummary({ schema, highlight }: { schema: DataSchema; highlight?: { dims: string[]; vars: string[] } }) {
  const dims = Object.entries(schema.dims ?? {});
  const vars = Object.entries(schema.vars ?? {});
  return (
    <div className="grid gap-2 text-xs">
      <div className="flex flex-wrap gap-1">
        {dims.map(([name, d]) => (
          <span
            key={name}
            className={cn(
              "rounded border px-1.5 py-0.5 font-mono",
              highlight?.dims.includes(name) ? "border-primary/50 bg-accent text-accent-foreground" : "bg-muted/60",
            )}
            title={`${d.kind}, ${d.policy ?? "fixed"}${d.freq ? `, ${d.freq}` : ""}`}
          >
            {name}
            <span className="text-muted-foreground">
              {" "}
              {d.kind}
              {d.freq ? ` ${d.freq}` : ""}
            </span>
          </span>
        ))}
      </div>
      <ul className="grid gap-0.5">
        {vars.map(([name, v]) => (
          <li key={name} className={cn("flex gap-2 font-mono", highlight?.vars.includes(name) && "text-primary")}>
            <span className="font-medium">{name}</span>
            <span className="text-muted-foreground">
              ({(v.dims ?? []).join(", ")}) {v.dtype} {v.role ?? "covariate"}
              {v.units ? ` [${v.units}]` : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StageBlock({ stage, open }: { stage: StageSchema | null; open: boolean }) {
  if (!open) return null;
  if (!stage) return <p className="text-xs text-muted-foreground">Validate to see the schema at this stage.</p>;
  if (!stage.data_schema) return <p className="text-xs text-warning">Dynamic effect: only a dry-run can resolve this stage.</p>;
  return <SchemaSummary schema={stage.data_schema} />;
}

function IssueChips({ issues, onIssue }: { issues: Issue[]; onIssue?: ((i: Issue) => void) | undefined }) {
  if (!issues.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {issues.map((i, n) => (
        <button
          key={n}
          type="button"
          onClick={() => onIssue?.(i)}
          className={cn(
            "rounded border px-1.5 py-0.5 text-left text-[11px]",
            (i.severity ?? "error") === "error" ? "border-destructive/40 text-destructive" : "border-warning/40 text-warning",
          )}
        >
          <span className="font-mono font-semibold">{i.code}</span> {i.message}
        </button>
      ))}
    </div>
  );
}

function SplitMarker() {
  return (
    <div className="relative my-1 flex items-center gap-2 py-1 text-xs font-medium text-primary" role="separator" aria-label="Train/holdout split">
      <Scissors className="size-3.5" />
      <span>Train / holdout split</span>
      <span aria-hidden className="h-px flex-1 border-t border-dashed border-primary/60" />
    </div>
  );
}

function StepCardView({ card, onIssue }: { card: StepCard; onIssue?: ((i: Issue) => void) | undefined }) {
  const [open, setOpen] = useState(false);
  const r = card.resolved;
  const hasError = card.issues.some((i) => (i.severity ?? "error") === "error");
  const highlight = card.delta ? { dims: [...card.delta.addedDims, ...card.delta.changedDims], vars: card.delta.addedVars } : undefined;
  return (
    <li className="relative pl-6">
      <span aria-hidden className="absolute top-0 bottom-0 left-[7px] w-px bg-border" />
      <span
        aria-hidden
        className={cn(
          "absolute top-3.5 left-0.5 size-[11px] rounded-full border-2 bg-card",
          hasError ? "border-destructive" : r?.stateful ? "border-primary" : "border-muted-foreground/50",
        )}
      />
      <div className={cn("rounded-lg border bg-card p-3", hasError && "border-destructive/40")}>
        <div className="flex flex-wrap items-center gap-2">
          <Mono className="font-semibold">{card.id}</Mono>
          <span className="truncate text-xs text-muted-foreground">{r?.resolved ?? card.use}</span>
          <span className="ml-auto flex flex-wrap gap-1">
            {r ? <Badge variant={KIND_VARIANT[r.kind]}>{r.kind}</Badge> : null}
            {r?.stateful ? <Badge variant="outline">learns state</Badge> : null}
            {r?.invertible ? <Badge variant="outline">invertible</Badge> : null}
            {r?.depends_on_target ? <Badge variant="warning">uses target</Badge> : null}
            <AboutButton kind="step" id={card.use} className="-my-1 size-6" />
          </span>
        </div>
        {r?.context ? (
          <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
            <History className="size-3.5" />
            Keeps a {r.context.n}-step tail along {r.context.along}
            {r.context.within?.length ? `, per ${r.context.within.join(", ")},` : ""} as scoring context.
          </p>
        ) : null}
        {card.delta &&
        (card.delta.addedDims.length || card.delta.removedDims.length || card.delta.changedDims.length || card.delta.addedVars.length || card.delta.removedVars.length) ? (
          <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px]">
            {card.delta.addedVars.map((v) => <span key={`+v${v}`} className="text-success">+{v}</span>)}
            {card.delta.removedVars.map((v) => <span key={`-v${v}`} className="text-destructive">−{v}</span>)}
            {card.delta.addedDims.map((d) => <span key={`+d${d}`} className="text-success">+dim {d}</span>)}
            {card.delta.changedDims.map((d) => <span key={`~d${d}`} className="text-info">~dim {d}</span>)}
            {card.delta.removedDims.map((d) => <span key={`-d${d}`} className="text-destructive">−dim {d}</span>)}
          </p>
        ) : null}
        <IssueChips issues={card.issues} onIssue={onIssue} />
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="mt-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
          Schema after this step
        </button>
        {open ? (
          <div className="mt-2">
            {card.stage?.data_schema ? (
              <SchemaSummary schema={card.stage.data_schema} {...(highlight ? { highlight } : {})} />
            ) : (
              <StageBlock stage={card.stage} open />
            )}
          </div>
        ) : null}
      </div>
      {card.splitAfter ? <SplitMarker /> : null}
    </li>
  );
}

/** The pipeline as a vertical list of step cards with schema propagation per stage. */
export function PipelineCards({ model, onIssue }: { model: PipelineModel; onIssue?: (i: Issue) => void }) {
  const [rawOpen, setRawOpen] = useState(false);
  return (
    <ol className="grid gap-2" aria-label="Pipeline">
      <li className="relative pl-6">
        <span aria-hidden className="absolute top-3.5 bottom-0 left-[7px] w-px bg-border" />
        <span aria-hidden className="absolute top-3.5 left-0.5 size-[11px] rounded-full bg-muted-foreground/40" />
        <div className="rounded-lg border border-dashed p-3">
          <button type="button" className="flex w-full items-center gap-2 text-left" onClick={() => setRawOpen((o) => !o)} aria-expanded={rawOpen}>
            <span className="font-medium">Raw data</span>
            <span className="text-xs text-muted-foreground">after binding, before any step</span>
            <ChevronDown className={cn("ml-auto size-3.5 transition-transform", rawOpen && "rotate-180")} />
          </button>
          {rawOpen ? <div className="mt-2"><StageBlock stage={model.raw} open /></div> : null}
        </div>
        {model.rawSplit ? <SplitMarker /> : null}
      </li>
      {model.steps.map((card) => (
        <StepCardView key={`${card.index}-${card.id}`} card={card} onIssue={onIssue} />
      ))}
      <li className="relative pl-6">
        <span aria-hidden className="absolute top-3.5 left-0.5 size-[11px] rounded-full bg-primary" />
        <div className="rounded-lg border bg-accent/50 p-3">
          <p className="font-medium">Model</p>
          <Mono className="text-xs text-muted-foreground">{model.modelUse ?? "not set"}</Mono>
          <AboutInline kind="model" id={model.modelUse} className="mt-1" />
          <IssueChips issues={model.modelIssues} onIssue={onIssue} />
        </div>
      </li>
    </ol>
  );
}
