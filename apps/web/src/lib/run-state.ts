/**
 * Presentation metadata for the run state machine (mapi_spec.run.RunState). Terminal and
 * resumable sets mirror the contract; the UI never invents transitions.
 */
import type { RunState } from "@/api/types";

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
  created: { label: "Created", tone: "neutral", description: "Accepted; waiting for static checks." },
  rejected: { label: "Rejected", tone: "danger", description: "Static validation or quota checks failed." },
  queued: { label: "Queued", tone: "neutral", description: "Waiting for an executor slot." },
  dispatching: { label: "Dispatching", tone: "active", description: "Being handed to the executor." },
  provisioning: { label: "Provisioning", tone: "active", description: "The sandbox is starting." },
  running: { label: "Running", tone: "active", description: "The runner is working." },
  stalled: {
    label: "Stalled",
    tone: "warning",
    description: "No events for a while. The executor still reports the run alive; this is a warning, not a failure.",
  },
  finalizing: { label: "Finalizing", tone: "active", description: "Verifying and copying outputs." },
  succeeded: { label: "Succeeded", tone: "success", description: "Outputs verified." },
  partial: { label: "Partial", tone: "warning", description: "Stopped with checkpoints; it can be resumed." },
  failed: { label: "Failed", tone: "danger", description: "The run failed; see the problem below." },
  cancelling: { label: "Cancelling", tone: "muted", description: "Waiting for the executor to confirm." },
  cancelled: { label: "Cancelled", tone: "muted", description: "Cancelled." },
};

export const TONE_CLASSES: Record<Tone, string> = {
  neutral: "bg-secondary text-secondary-foreground border-border",
  active: "bg-info/12 text-info border-info/30",
  success: "bg-success/12 text-success border-success/30",
  warning: "bg-warning/14 text-warning border-warning/35",
  danger: "bg-destructive/12 text-destructive border-destructive/30",
  muted: "bg-muted text-muted-foreground border-border",
};

export const RESOURCE_CLASSES = ["S", "M", "L", "XL"] as const;

/** The next resource class up, for the "retry one class up" offer after RESOURCE_EXHAUSTED. */
export function nextResourceClass(rc: string): (typeof RESOURCE_CLASSES)[number] | null {
  const i = RESOURCE_CLASSES.indexOf(rc as (typeof RESOURCE_CLASSES)[number]);
  return i >= 0 && i < RESOURCE_CLASSES.length - 2 ? (RESOURCE_CLASSES[i + 1] ?? null) : null;
}
