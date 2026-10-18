/**
 * Validation states and check outcomes in plain words, and the state machine
 * (`mapi_spec.governance.TRANSITIONS`) the timeline draws.
 */
import type { CheckSeverity, CheckStatus, ValidationEvent, ValidationStatus } from "./types";

type Tone = "neutral" | "active" | "success" | "warning" | "danger" | "muted";

export const VALIDATION_META: Record<ValidationStatus, { label: string; tone: Tone; plain: string }> = {
  draft: { label: "Draft", tone: "neutral", plain: "Nobody submitted it." },
  checks_running: { label: "Checks running", tone: "active", plain: "The checks run in the sandbox now." },
  checks_passed: { label: "Checks passed", tone: "success", plain: "Every blocking check passed." },
  checks_failed: { label: "Checks failed", tone: "danger", plain: "A blocking check failed or did not calculate a result." },
  awaiting_approval: { label: "Awaiting approval", tone: "warning", plain: "The checks passed. A reviewer must approve it." },
  validated: { label: "Validated", tone: "success", plain: "You can use the model. You can also promote it where a policy needs validation." },
  rejected: { label: "Rejected", tone: "danger", plain: "A reviewer, or the failed checks, rejected it." },
  deprecated: { label: "Deprecated", tone: "muted", plain: "It was validated before. Do not use it now." },
};

export const TONE_BADGE: Record<Tone, "secondary" | "info" | "success" | "warning" | "danger" | "outline"> = {
  neutral: "secondary",
  active: "info",
  success: "success",
  warning: "warning",
  danger: "danger",
  muted: "outline",
};

export const CHECK_META: Record<CheckStatus, { label: string; tone: Tone; plain: string }> = {
  pass: { label: "Pass", tone: "success", plain: "Passed" },
  warn: { label: "Warning", tone: "warning", plain: "Passed with a warning" },
  fail: { label: "Fail", tone: "danger", plain: "Failed" },
  error: { label: "Error", tone: "danger", plain: "Did not calculate a result" },
  skipped: { label: "Skipped", tone: "muted", plain: "Did not apply to this model" },
};

export const SEVERITY_META: Record<CheckSeverity, { label: string; plain: string }> = {
  blocking: { label: "Blocking", plain: "A failure stops validation" },
  warning: { label: "Warning", plain: "The reviewer sees it. It does not stop validation" },
  info: { label: "Information", plain: "Evidence only" },
};

export const EVENT_LABEL: Record<ValidationEvent, string> = {
  submit: "Submitted",
  cancel: "Cancelled",
  checks_passed: "Checks passed",
  checks_failed: "Checks failed",
  request_approval: "Sent for approval",
  validate: "Validated",
  approve: "Approved",
  reject: "Rejected",
  resubmit: "Resubmitted",
  deprecate: "Deprecated",
};

export const TRANSITIONS: Record<string, ValidationStatus> = {
  "draft:submit": "checks_running",
  "checks_running:checks_passed": "checks_passed",
  "checks_running:checks_failed": "checks_failed",
  "checks_running:cancel": "draft",
  "checks_passed:request_approval": "awaiting_approval",
  "checks_passed:validate": "validated",
  "checks_failed:resubmit": "checks_running",
  "checks_failed:reject": "rejected",
  "awaiting_approval:approve": "validated",
  "awaiting_approval:reject": "rejected",
  "rejected:resubmit": "checks_running",
  "validated:deprecate": "deprecated",
};

/** Events allowed from a status (what the page offers next). */
export function nextEvents(status: ValidationStatus): ValidationEvent[] {
  return Object.keys(TRANSITIONS)
    .filter((k) => k.startsWith(`${status}:`))
    .map((k) => k.split(":")[1] as ValidationEvent);
}

/** The happy path the timeline draws; the current status is marked on it (or beside it). */
export const MAIN_PATH: ValidationStatus[] = ["draft", "checks_running", "checks_passed", "awaiting_approval", "validated"];
