/**
 * Workflow types: aliases of the generated OpenAPI components (`mapi_spec.workflow`,
 * `.workflow_nodes`, `.workflow_templates`, `mapi_spec.about`) and the `WorkflowSpec` document,
 * which the API carries as free JSON (`document: Record<JsonValue>`), typed here after
 * `mapi_spec.workflow.WorkflowSpec`.
 */
import type { components } from "@/api/schema";

type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type About = S["About"];
export type AboutKind = S["AboutKind"];
export type CatalogAbout = S["CatalogAbout"];
export type Difficulty = S["Difficulty"];
export type TypicalRuntime = S["TypicalRuntime"];

export type Workflow = S["Workflow"];
export type WorkflowCreate = S["WorkflowCreate"];
export type WorkflowRevision = S["WorkflowRevision"];
export type WorkflowRevisionCreate = S["WorkflowRevisionCreate"];
export type WorkflowValidation = S["WorkflowValidation"];
export type WorkflowTemplate = S["WorkflowTemplate"];
export type NodeImplementation = S["NodeImplementation"];
export type NodeKind = S["NodeKind"];
export type PortSpec = S["PortSpec"];
export type PortType = S["PortType"];
export type ResultField = S["ResultField"];
export type WorkflowRun = S["WorkflowRun"];
export type WorkflowRunCreate = S["WorkflowRunCreate"];
export type WorkflowRunStatus = S["WorkflowRunStatus"];
export type NodeRun = S["NodeRun"];
export type NodeRunStatus = S["NodeRunStatus"];
export type NodeOutputRef = S["NodeOutputRef"];
export type NodeError = S["NodeError"];
export type BranchDecision = S["BranchDecision"];
export type WorkflowEvent = S["WorkflowEvent"];
export type WorkflowEventType = S["WorkflowEventType"];
export type WorkflowTriggerEvent = S["WorkflowTriggerEvent"];
export type WorkflowSchedule = S["WorkflowSchedule"];
export type ScheduleCreate = S["ScheduleCreate"];
export type ScheduleUpdate = S["ScheduleUpdate"];
export type WorkflowTrigger = S["WorkflowTrigger"];
export type TriggerCreate = S["TriggerCreate"];

/** A JSON value (`pydantic.JsonValue`). */
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

/** `inputs.<name>` or `<node>.<port>`, or a list of alternatives / collected sources. */
export type Binding = string | string[];

/** `mapi_spec.workflow.WorkflowParam`. */
export interface WorkflowParamDecl {
  type: "number" | "integer" | "boolean" | "string" | "array" | "object";
  default?: Json;
  required?: boolean;
  description?: string | null;
  minimum?: number | null;
  maximum?: number | null;
  enum?: Json[] | null;
}

/** `mapi_spec.workflow.WorkflowInput`. */
export interface WorkflowInputDecl {
  type: PortType;
  required?: boolean;
  default?: string | null;
  description?: string | null;
}

/** `mapi_spec.workflow.WorkflowNode` (`with` is the parameter block). */
export interface WorkflowNodeDoc {
  id: string;
  use: string;
  kind?: NodeKind | null;
  with?: JsonObject;
  inputs?: Record<string, Binding>;
  when?: string | null;
  label?: string | null;
  description?: string | null;
  retries?: number;
  timeout?: string | null;
  cache?: boolean;
  on_failure?: "fail" | "continue";
  resource_class?: string | null;
}

/** `mapi_spec.workflow.WorkflowSpec`. */
export interface WorkflowDoc {
  apiVersion: string;
  kind: "Workflow";
  metadata: { name: string; project: string; labels?: Record<string, string>; description?: string | null; about?: About | null };
  params?: Record<string, WorkflowParamDecl>;
  inputs?: Record<string, WorkflowInputDecl>;
  nodes: WorkflowNodeDoc[];
  outputs?: Record<string, string>;
  execution?: { max_parallel?: number; resource_class?: string; timeout?: string };
}

/** The pseudo-node id under which the workflow inputs appear on the canvas. */
export const INPUTS_NODE = "inputs";

export const NODE_KINDS: readonly NodeKind[] = [
  "test",
  "transform",
  "branch",
  "split",
  "search",
  "model",
  "evaluate",
  "compare",
  "select",
  "governance",
  "report",
  "plot",
  "subworkflow",
];

export const PORT_TYPES: readonly PortType[] = [
  "dataset",
  "test_result",
  "split_plan",
  "model_version",
  "metrics",
  "table",
  "plot",
  "report",
  "evidence",
];

export const BRANCH_NODE = "branch.if";
export const SUBWORKFLOW_NODE = "subworkflow.run";
