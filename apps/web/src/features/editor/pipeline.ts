/**
 * Pipeline view model: joins the spec's `pipeline` (from the YAML being edited) with the
 * server's static validation (per-stage schemas, resolved steps, issues).
 */
import type { DataSchema, Issue, ResolvedStep, StageSchema, ValidateResponse } from "@/api/types";
import { pointerWithin } from "@/lib/pointer";

export interface SchemaDelta {
  addedDims: string[];
  removedDims: string[];
  changedDims: string[];
  addedVars: string[];
  removedVars: string[];
}

export interface StepCard {
  index: number;
  id: string;
  use: string;
  stage: StageSchema | null;
  resolved: ResolvedStep | null;
  /** The train/holdout split happens right after this step. */
  splitAfter: boolean;
  delta: SchemaDelta | null;
  issues: Issue[];
}

export interface PipelineModel {
  raw: StageSchema | null;
  rawSplit: boolean;
  steps: StepCard[];
  modelUse: string | null;
  modelIssues: Issue[];
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

export function schemaDelta(before: DataSchema | null | undefined, after: DataSchema | null | undefined): SchemaDelta | null {
  if (!before || !after) return null;
  const bd = before.dims ?? {};
  const ad = after.dims ?? {};
  const bv = before.vars ?? {};
  const av = after.vars ?? {};
  return {
    addedDims: Object.keys(ad).filter((d) => !(d in bd)),
    removedDims: Object.keys(bd).filter((d) => !(d in ad)),
    changedDims: Object.keys(ad).filter((d) => d in bd && JSON.stringify(ad[d]) !== JSON.stringify(bd[d])),
    addedVars: Object.keys(av).filter((v) => !(v in bv)),
    removedVars: Object.keys(bv).filter((v) => !(v in av)),
  };
}

export function buildPipeline(document: unknown, result: ValidateResponse | null): PipelineModel {
  const doc = isObj(document) ? document : {};
  const pipeline = Array.isArray(doc.pipeline) ? doc.pipeline : [];
  const stages = new Map((result?.stages ?? []).map((s) => [s.name, s]));
  const resolvedSteps = new Map((result?.resolved?.steps ?? []).map((s) => [s.id, s]));
  const splitStage = result?.resolved?.split_stage ?? result?.stages?.find((s) => s.split)?.name ?? null;
  const issues = result?.issues ?? [];
  const raw = stages.get("raw") ?? null;

  let previous: DataSchema | null | undefined = raw?.data_schema;
  const steps: StepCard[] = pipeline.map((entry, index) => {
    const e = isObj(entry) ? entry : {};
    const id = typeof e.id === "string" ? e.id : `step ${index + 1}`;
    const stage = stages.get(id) ?? null;
    const card: StepCard = {
      index,
      id,
      use: typeof e.use === "string" ? e.use : "",
      stage,
      resolved: resolvedSteps.get(id) ?? null,
      splitAfter: splitStage === id || Boolean(stage?.split),
      delta: schemaDelta(previous, stage?.data_schema),
      issues: issues.filter((i) => pointerWithin(i.pointer ?? "", `/pipeline/${index}`)),
    };
    previous = stage?.data_schema;
    return card;
  });
  const model = isObj(doc.model) ? doc.model : {};
  return {
    raw,
    rawSplit: splitStage === "raw" || Boolean(raw?.split),
    steps,
    modelUse: typeof model.use === "string" ? model.use : null,
    modelIssues: issues.filter((i) => pointerWithin(i.pointer ?? "", "/model")),
  };
}
