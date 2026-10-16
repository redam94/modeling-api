/**
 * Where a model version comes from: the dataset version it was fitted on, the spec revision,
 * the fit run and (when a workflow fitted it) the workflow run, plus the reproducibility
 * facts of its lineage (plugin, image, seed, libraries).
 */
import { Link } from "@tanstack/react-router";
import { ArrowDown, Boxes, Database, FileCode2, GitBranch, Play } from "lucide-react";
import type { ReactNode } from "react";

import { useRun, useSpecRevision } from "@/api/queries";
import type { ModelVersion } from "@/api/types";
import { KeyValues, Mono, StateBadge } from "@/components/common";
import { Skeleton } from "@/components/ui/primitives";
import { shortId } from "@/lib/format";

function Step({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[2rem_1fr] items-start gap-3">
      <span aria-hidden className="flex size-8 items-center justify-center rounded-lg border bg-accent text-primary [&_svg]:size-4">
        {icon}
      </span>
      <div className="grid min-w-0 gap-0.5 pt-0.5">
        <p className="text-xs font-medium text-muted-foreground">{title}</p>
        <div className="min-w-0 text-[13px] break-words">{children}</div>
      </div>
    </li>
  );
}

function Arrow() {
  return (
    <li aria-hidden className="pl-2.5 text-muted-foreground">
      <ArrowDown className="size-3.5" />
    </li>
  );
}

/** The `data.dataset` reference of a spec document (`dataset://project/name@v3`). */
export function datasetRefOf(document: Record<string, unknown> | undefined): string | null {
  const data = document?.data;
  if (typeof data === "object" && data !== null && !Array.isArray(data)) {
    const ref = (data as Record<string, unknown>).dataset;
    return typeof ref === "string" ? ref : null;
  }
  return null;
}

export function LineagePanel({ mv, expert }: { mv: ModelVersion; expert: boolean }) {
  const run = useRun(mv.run_id);
  const parent = useRun(run.data?.parent_run_id ?? undefined);
  const spec = useSpecRevision(run.data?.spec_id ?? undefined, run.data?.spec_revision ?? undefined);
  const lin = mv.lineage;
  const datasetRef = datasetRefOf(spec.data?.document);
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <ol className="grid content-start gap-2" aria-label="Lineage">
        <Step icon={<Database />} title="Dataset">
          {run.isPending ? (
            <Skeleton className="h-5 w-48" />
          ) : run.data?.dataset_id ? (
            <Link to="/datasets/$datasetId" params={{ datasetId: run.data.dataset_id }} className="text-primary hover:underline">
              {datasetRef ?? run.data.dataset_id}
            </Link>
          ) : (
            <span>{datasetRef ?? "—"}</span>
          )}
          {lin.dataset_sha ? <Mono className="block text-muted-foreground">data {shortId(lin.dataset_sha)}</Mono> : null}
        </Step>
        <Arrow />
        <Step icon={<FileCode2 />} title="Spec">
          {run.data?.spec_id ? (
            <Link to="/build/$specId" params={{ specId: run.data.spec_id }} search={{ rev: run.data.spec_revision ?? undefined }} className="text-primary hover:underline">
              {spec.data?.ref ?? `${run.data.spec_id} r${run.data.spec_revision ?? "?"}`}
            </Link>
          ) : (
            <span className="text-muted-foreground">Not from a stored spec</span>
          )}
          <Mono className="block text-muted-foreground">spec {shortId(lin.spec_hash)}</Mono>
        </Step>
        <Arrow />
        <Step icon={<Play />} title="Fit run">
          <span className="flex flex-wrap items-center gap-2">
            <Link to="/runs/$runId" params={{ runId: mv.run_id }} className="text-primary hover:underline">
              {shortId(mv.run_id)}
            </Link>
            {run.data ? <StateBadge state={run.data.state} /> : null}
            {lin.attempt > 1 ? <span className="text-xs text-muted-foreground">attempt {lin.attempt}</span> : null}
          </span>
        </Step>
        {run.data?.parent_run_id ? (
          <>
            <Arrow />
            <Step icon={<GitBranch />} title={parent.data?.kind === "workflow" ? "Workflow run" : "Parent run"}>
              <span className="flex flex-wrap items-center gap-2">
                <Link to="/runs/$runId" params={{ runId: run.data.parent_run_id }} className="text-primary hover:underline">
                  {shortId(run.data.parent_run_id)}
                </Link>
                {parent.data ? <StateBadge state={parent.data.state} /> : null}
              </span>
            </Step>
          </>
        ) : null}
        <Arrow />
        <Step icon={<Boxes />} title="Model version">
          <Mono>{mv.ref}</Mono>
        </Step>
      </ol>
      <div className="grid content-start gap-3">
        <h3 className="text-[13px] font-semibold">How to reproduce it</h3>
        <KeyValues
          items={[
            ["Plugin", lin.plugin ? <Mono key="p">{lin.plugin}</Mono> : "—"],
            ["Engine", lin.engine ?? "—"],
            ["Seed", lin.seed ?? "—"],
            ["Base image", lin.base_image ? <Mono key="b">{lin.base_image}</Mono> : "—"],
            ...(expert
              ? ([
                  ["Executor", lin.executor ?? "—"],
                  ["Resource class", lin.resource_class ?? "—"],
                  ["Catalog digest", lin.catalog_digest ? <Mono key="c">{shortId(lin.catalog_digest)}</Mono> : "—"],
                  ["Side tables", Object.keys(lin.side_tables ?? {}).join(", ") || "—"],
                  ["Libraries", Object.entries(lin.lib_versions ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || "—"],
                ] as [ReactNode, ReactNode][])
              : []),
          ]}
        />
      </div>
    </div>
  );
}
