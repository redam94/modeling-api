/**
 * Presentation metadata for the run state machine (mapi_spec.run.RunState). Terminal and
 * resumable sets mirror the contract; the UI never invents transitions.
 */
import type { Run, RunKind, RunState } from "@/api/types";
import { shortId } from "@/lib/format";

export const RUN_KINDS: readonly RunKind[] = ["fit", "validate", "score", "ingest", "evaluate", "derive", "render"];

export const RUN_STATES: readonly RunState[] = [
  "created",
  "rejected",
  "queued",
  "dispatching",
  "provisioning",
  "running",
  "stalled",
  "finalizing",
  "succeeded",
  "partial",
  "failed",
  "cancelling",
  "cancelled",
];

const TERMINAL: ReadonlySet<RunState> = new Set(["rejected", "succeeded", "partial", "failed", "cancelled"]);

export function isTerminal(state: RunState): boolean {
  return TERMINAL.has(state);
}

/**
 * States in which `:cancel` makes sense (it is idempotent server-side anyway). A finalizing
 * run's execution is already over, so the server leaves it alone.
 */
export function isCancellable(state: RunState): boolean {
  return !TERMINAL.has(state) && state !== "cancelling" && state !== "finalizing";
}

export type Tone = "neutral" | "active" | "success" | "warning" | "danger" | "muted";

export interface StateMeta {
  label: string;
  tone: Tone;
  description: string;
}

export const STATE_META: Record<RunState, StateMeta> = {
  created: { label: "Created", tone: "neutral", description: "The server accepted the run. The run waits for the static checks." },
  rejected: { label: "Rejected", tone: "danger", description: "The static validation or a quota check failed." },
  queued: { label: "Queued", tone: "neutral", description: "The run waits for a free executor slot." },
  dispatching: { label: "Dispatching", tone: "active", description: "The server sends the run to the executor." },
  provisioning: { label: "Provisioning", tone: "active", description: "The sandbox starts." },
  running: { label: "Running", tone: "active", description: "The runner does the work." },
  stalled: {
    label: "Stalled",
    tone: "warning",
    description: "There are no events for some time. The executor still reports that the run is alive. This is a warning, not a failure.",
  },
  finalizing: { label: "Finalizing", tone: "active", description: "The runner verifies and copies the outputs." },
  succeeded: { label: "Succeeded", tone: "success", description: "The outputs are verified." },
  partial: { label: "Partial", tone: "warning", description: "The run stopped with checkpoints. You can resume it." },
  failed: { label: "Failed", tone: "danger", description: "The run failed. Look at the problem below." },
  cancelling: { label: "Cancelling", tone: "muted", description: "The run waits for the executor to confirm the cancel." },
  cancelled: { label: "Cancelled", tone: "muted", description: "The run was cancelled." },
};

export const TONE_CLASSES: Record<Tone, string> = {
  neutral: "bg-secondary text-secondary-foreground border-border",
  active: "bg-info/12 text-info border-info/30",
  success: "bg-success/12 text-success border-success/30",
  warning: "bg-warning/14 text-warning border-warning/35",
  danger: "bg-destructive/12 text-destructive border-destructive/30",
  muted: "bg-muted text-muted-foreground border-border",
};

/** The resource classes, smallest first. v0.15: `GPU` (one NVIDIA L4) is the last one. */
export const RESOURCE_CLASSES = ["S", "M", "L", "XL", "GPU"] as const;
export type ResourceClassName = (typeof RESOURCE_CLASSES)[number];

/** The GPU class (v0.15). The "one class up" ladder never offers it. */
export const GPU_CLASS = "GPU" satisfies ResourceClassName;
/** The help text of the GPU class wherever a resource class is selected or listed. */
export const GPU_CLASS_HELP = "One NVIDIA L4 GPU. Only for models that use a GPU.";
/** The classes a run can retry at (XL needs an operator; GPU only for models that use a GPU). */
export const RETRY_CLASSES = ["S", "M", "L", "GPU"] as const satisfies readonly ResourceClassName[];
const CPU_LADDER: readonly ResourceClassName[] = ["S", "M", "L"];

/** A short label of a resource class (`GPU` names its accelerator). */
export function resourceClassLabel(rc: string): string {
  return rc === GPU_CLASS ? "GPU (NVIDIA L4)" : rc;
}

/** The next resource class up, for the "retry one class up" offer after RESOURCE_EXHAUSTED. */
export function nextResourceClass(rc: string): ResourceClassName | null {
  const i = CPU_LADDER.indexOf(rc as ResourceClassName);
  return i >= 0 && i < CPU_LADDER.length - 1 ? (CPU_LADDER[i + 1] ?? null) : null;
}

/** What a run ran on, as a short label (spec revision, model version or dataset). */
export function runSubject(run: Run): string {
  if (run.spec_id) return `${shortId(run.spec_id)} r${run.spec_revision ?? "?"}`;
  if (run.model_version_id) return shortId(run.model_version_id);
  if (run.dataset_id) return shortId(run.dataset_id);
  return "—";
}
