/** Plain-language labels and tones for workflow-run and node-run states. */
import type { NodeKind, NodeRunStatus, WorkflowRunStatus, WorkflowTriggerEvent } from "./types";

export type Tone = "neutral" | "active" | "success" | "warning" | "danger" | "muted";

export const TONE_BADGE: Record<Tone, "secondary" | "info" | "success" | "warning" | "danger" | "outline"> = {
  neutral: "secondary",
  active: "info",
  success: "success",
  warning: "warning",
  danger: "danger",
  muted: "outline",
};

/** Border and text colour of a canvas node in a run. */
export const TONE_NODE: Record<Tone, string> = {
  neutral: "border-border",
  active: "border-info ring-2 ring-info/30",
  success: "border-success",
  warning: "border-warning",
  danger: "border-destructive ring-2 ring-destructive/25",
  muted: "border-dashed border-border opacity-70",
};

export const NODE_STATUS_META: Record<NodeRunStatus, { label: string; tone: Tone; plain: string }> = {
  pending: { label: "Waiting", tone: "neutral", plain: "Has not started yet" },
  queued: { label: "Queued", tone: "active", plain: "Waits for a worker" },
  running: { label: "Running", tone: "active", plain: "Running now" },
  succeeded: { label: "Done", tone: "success", plain: "Finished" },
  cached: { label: "Reused", tone: "success", plain: "Nothing changed. The server used the earlier result again" },
  skipped: { label: "Skipped", tone: "muted", plain: "Not needed on this path" },
  failed: { label: "Failed", tone: "danger", plain: "Stopped with an error" },
  cancelled: { label: "Cancelled", tone: "muted", plain: "Stopped because the run was cancelled" },
};

export const RUN_STATUS_META: Record<WorkflowRunStatus, { label: string; tone: Tone }> = {
  queued: { label: "Queued", tone: "active" },
  running: { label: "Running", tone: "active" },
  succeeded: { label: "Succeeded", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  cancelling: { label: "Cancelling", tone: "warning" },
  cancelled: { label: "Cancelled", tone: "muted" },
};

export const TRIGGER_LABEL: Record<WorkflowTriggerEvent, string> = {
  manual: "Started manually",
  schedule: "On a schedule",
  "dataset.version.created": "New data arrived",
  parent: "From a parent workflow",
  validation: "From a validation request",
};

export const KIND_LABEL: Record<NodeKind, string> = {
  test: "Test",
  transform: "Data processing",
  branch: "Decision",
  split: "Train/test plan",
  search: "Search",
  model: "Model",
  evaluate: "Evaluation",
  compare: "Comparison",
  select: "Selection",
  governance: "Governance",
  report: "Report",
  plot: "Plot",
  subworkflow: "Sub-workflow",
};

/** Accent per kind (theme series tokens), for the canvas chip. */
export const KIND_COLOR: Record<NodeKind, string> = {
  test: "var(--series-3)",
  transform: "var(--series-5)",
  branch: "var(--series-4)",
  split: "var(--series-6)",
  search: "var(--series-7)",
  model: "var(--series-1)",
  evaluate: "var(--series-2)",
  compare: "var(--series-2)",
  select: "var(--series-8)",
  governance: "var(--warning)",
  report: "var(--series-8)",
  plot: "var(--series-5)",
  subworkflow: "var(--series-1)",
};
