/**
 * A Guided session is a stored spec (plan §11: "both modes edit the same ModelSpec").
 *
 * `POST /v1/guided/drafts` builds the draft and stores nothing; once the data is uploaded the
 * draft is saved with `POST /v1/specs` (its `metadata.labels.guided` names the template) and
 * every later step writes a new revision (`Guided: …` messages). Everything a step needs on
 * resume is derived from server state: the spec and its revisions, the dataset it names, the
 * fit runs of the spec, the model version the latest fit produced and the endpoints serving it.
 * Only the answers of a session that has not uploaded its data yet live in the browser
 * (sessionStorage), because the file itself has to be chosen again anyway.
 */
import { parsePointer } from "@/lib/pointer";

import type { GuidedTemplate, SpecDocument } from "./types";

export const GUIDED_LABEL = "guided";
export const GUIDED_MESSAGE = "Guided:";

export const STEP_IDS = ["data", "priors", "run", "report", "explore", "share"] as const;
export type GuidedStepId = (typeof STEP_IDS)[number];

export interface StepDef {
  id: GuidedStepId;
  title: string;
  /** What the step answers, for the stepper and the page header. */
  question: string;
}

export function hasTask(template: Pick<GuidedTemplate, "tasks">, task: NonNullable<GuidedTemplate["tasks"]>[number]): boolean {
  return (template.tasks ?? []).includes(task);
}

export function exploreTitle(template: Pick<GuidedTemplate, "tasks" | "family">): string {
  if (hasTask(template, "whatif")) return "What if…";
  if (hasTask(template, "forecast")) return "Forecast";
  return "Scenarios";
}

/** The steps of a template's flow (business priors only where the template takes them). */
export function stepsFor(template: Pick<GuidedTemplate, "tasks" | "family">): StepDef[] {
  const steps: StepDef[] = [{ id: "data", title: "Your data", question: "Which columns hold what?" }];
  if (hasTask(template, "business_priors") || hasTask(template, "lift_tests")) {
    steps.push({ id: "priors", title: "What you believe", question: "What do you already know about your channels?" });
  }
  steps.push(
    { id: "run", title: "Fit the model", question: "Let the model learn from your data" },
    { id: "report", title: "Results", question: "What did the model find, and can you trust it?" },
    { id: "explore", title: exploreTitle(template), question: hasTask(template, "whatif") ? "What happens if you change the budget?" : "What comes next?" },
    { id: "share", title: "Share", question: "Put the result to work" },
  );
  return steps;
}

export function isStepId(v: unknown): v is GuidedStepId {
  return typeof v === "string" && (STEP_IDS as readonly string[]).includes(v);
}

// ----------------------------------------------------------------------------- the document

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const ABSENT = Symbol("absent");

/** The value at a JSON pointer (`ABSENT` when it is not there). */
export function valueAt(doc: unknown, pointer: string): unknown {
  let node: unknown = doc;
  for (const part of parsePointer(pointer)) {
    if (isRecord(node) && part in node) node = node[part];
    else if (Array.isArray(node) && /^\d+$/.test(part) && Number(part) < node.length) node = node[Number(part)];
    else return ABSENT;
  }
  return node;
}

/** `metadata.labels.guided`: the template a spec was drafted from. */
export function guidedTemplateOf(doc: SpecDocument | null | undefined): string | null {
  const labels = valueAt(doc, "/metadata/labels");
  return isRecord(labels) && typeof labels[GUIDED_LABEL] === "string" ? labels[GUIDED_LABEL] : null;
}

/** `dataset://<project>/<name>@…` → `name`. */
export function datasetNameOf(doc: SpecDocument | null | undefined): string | null {
  const ref = valueAt(doc, "/data/dataset");
  if (typeof ref !== "string") return null;
  const m = /^dataset:\/\/[^/]+\/([^@/]+)(?:@.*)?$/.exec(ref);
  return m ? (m[1] ?? null) : null;
}

export function specName(doc: SpecDocument | null | undefined): string | null {
  const v = valueAt(doc, "/metadata/name");
  return typeof v === "string" ? v : null;
}

function stable(v: unknown): string {
  if (v === ABSENT) return "<absent>";
  if (Array.isArray(v)) return `[${v.map(stable).join(",")}]`;
  if (isRecord(v)) {
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stable(v[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(v) ?? "null";
}

/**
 * Pointers of the template's `advanced` and `hidden` settings whose value in `doc` differs
 * from `reference` (the revision Guided mode drafted). Guided mode keeps every such value as it
 * is and marks the session "advanced settings applied".
 */
export function advancedChanges(template: Pick<GuidedTemplate, "ui_levels">, doc: SpecDocument, reference: SpecDocument | null): string[] {
  if (!reference) return [];
  return Object.entries(template.ui_levels ?? {})
    .filter(([, level]) => level !== "basic")
    .map(([pointer]) => pointer)
    .filter((pointer) => stable(valueAt(doc, pointer)) !== stable(valueAt(reference, pointer)))
    .sort();
}

/** The dim the template's date question binds (`time` for most, `t` for the weekly forecast). */
export function timeDimOf(template: Pick<GuidedTemplate, "spec_template">): string {
  const dims = valueAt(template.spec_template, "/data/bind/dims");
  if (isRecord(dims)) {
    for (const [k, v] of Object.entries(dims)) if (v === "{{date}}") return k;
  }
  return "time";
}

/** The wide variable and dim the template's spend (or price) question builds. */
export function wideVarOf(template: Pick<GuidedTemplate, "questions">): { variable: string; dim: string; question: string } | null {
  const q = template.questions.find((x) => x.kind === "columns" && x.wide_dim && x.as_var && x.required !== false);
  return q && q.as_var && q.wide_dim ? { variable: q.as_var, dim: q.wide_dim, question: q.id } : null;
}

// ----------------------------------------------------------------------------- names

/** A name that fits `mapi_spec._types.SLUG_RE` (lower-case letters, digits, `-`, `_`). */
export function slugify(text: string, fallback = "analysis"): string {
  const s = text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "")
    .slice(0, 100)
    .replace(/[-_]+$/g, "");
  return s || fallback;
}

export function defaultSessionName(templateId: string, now: Date = new Date()): string {
  const d = now.toISOString().slice(0, 10).replace(/-/g, "");
  return slugify(`${templateId.replace(/_/g, "-")}-${d}`);
}

// ----------------------------------------------------------------------------- local answers

export interface LocalDraft {
  name: string;
  answers: Record<string, unknown>;
  savedAt: string;
}

const KEY = (templateId: string) => `mapi.guided.draft.${templateId}`;

/** The answers of an unsaved session (per template, this tab only). */
export function loadLocalDraft(templateId: string): LocalDraft | null {
  try {
    const raw = sessionStorage.getItem(KEY(templateId));
    if (!raw) return null;
    const v = JSON.parse(raw) as unknown;
    return isRecord(v) && typeof v.name === "string" && isRecord(v.answers) ? { name: v.name, answers: v.answers, savedAt: typeof v.savedAt === "string" ? v.savedAt : "" } : null;
  } catch {
    return null;
  }
}

export function saveLocalDraft(templateId: string, draft: Omit<LocalDraft, "savedAt">): void {
  try {
    sessionStorage.setItem(KEY(templateId), JSON.stringify({ ...draft, savedAt: new Date().toISOString() }));
  } catch {
    // storage unavailable (private mode): answers live as long as the page
  }
}

export function clearLocalDraft(templateId: string): void {
  try {
    sessionStorage.removeItem(KEY(templateId));
  } catch {
    // ignore
  }
}
