/**
 * "Save steps as preset" (v0.18 D4): keep a spec's preparation steps as a team prep preset.
 * Mounted by the visual spec builder (WP-W3). The columns each step names can become slots, so
 * the preset works on another dataset: people pick their own columns when they use it. The
 * column choices are on by default (they change most between datasets), dims are off.
 */
import { Check, Save } from "lucide-react";
import { useMemo, useState } from "react";

import { can } from "@/api/access";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { useGrants } from "@/features/auth/workspace";

import { useSaveDoc } from "./actions";
import { identifier, newPrepPreset, slotValue } from "./doc";
import { useStepCatalog } from "./catalog";
import { LibraryLink, MetadataFields, PILL, ScopeChoice, ToggleChip } from "./ui";
import { MAX_SLOTS, splitUseRef, type Json, type LibraryMetadata, type LibraryScope, type PrepPresetDoc, type Slot, type StepCallDoc } from "./types";

export interface SaveStepsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The spec's pipeline steps (`id`, `use`, `with`). */
  steps: readonly StepCallDoc[];
}

interface Candidate {
  step: number;
  key: string;
  kind: Slot["kind"];
  label: string;
}

/** The step settings that name columns or dims and have a value (what can become a slot). */
function candidatesOf(steps: readonly StepCallDoc[], catalog: ReturnType<typeof useStepCatalog>): Candidate[] {
  const out: Candidate[] = [];
  steps.forEach((s, i) => {
    const entry = catalog.byId.get(splitUseRef(s.use)[0]);
    const w = (s.with ?? {}) as Record<string, Json>;
    const title = entry?.about?.title ?? s.id;
    for (const key of entry?.var_params ?? []) if (w[key] != null) out.push({ step: i, key, kind: Array.isArray(w[key]) ? "var_list" : "var", label: `${title}: which column${Array.isArray(w[key]) ? "s" : ""} (${key})` });
    for (const key of entry?.dim_params ?? []) if (w[key] != null && !out.some((c) => c.step === i && c.key === key)) out.push({ step: i, key, kind: "dim", label: `${title}: which dimension (${key})` });
  });
  return out;
}

/** The prep preset of `steps`, with the chosen settings turned into slots. */
export function presetFromSteps(steps: readonly StepCallDoc[], chosen: readonly Candidate[], metadata: LibraryMetadata): PrepPresetDoc {
  const doc = newPrepPreset("");
  const slots: Record<string, Slot> = {};
  const out = steps.map((s) => ({ ...s, with: { ...((s.with ?? {})) } }));
  for (const c of chosen) {
    let name = identifier(c.key);
    for (let n = 2; name in slots; n++) name = identifier(`${c.key}_${n}`);
    slots[name] = { kind: c.kind, label: c.label.slice(0, 80) };
    const step = out[c.step];
    if (step) step.with[c.key] = slotValue(name, c.kind === "var_list");
  }
  return { ...doc, metadata, slots, steps: out.map((s) => ({ id: s.id, use: s.use, ...(Object.keys(s.with).length ? { with: s.with } : {}) })) };
}

function Body({ steps, onDone }: { steps: readonly StepCallDoc[]; onDone: () => void }) {
  const grants = useGrants();
  const catalog = useStepCatalog();
  const candidates = useMemo(() => candidatesOf(steps, catalog), [steps, catalog]);
  const [off, setOff] = useState<Set<string>>(() => new Set());
  const [on, setOn] = useState<Set<string>>(() => new Set());
  const [metadata, setMetadata] = useState<LibraryMetadata>(() => newPrepPreset("").metadata);
  const [scope, setScope] = useState<LibraryScope>("project");
  const [savedId, setSavedId] = useState<string | null>(null);
  const save = useSaveDoc(null);
  const keyOf = (c: Candidate) => `${c.step}:${c.key}`;
  const isChosen = (c: Candidate) => (c.kind === "dim" ? on.has(keyOf(c)) : !off.has(keyOf(c)));
  const chosen = candidates.filter(isChosen).slice(0, MAX_SLOTS);
  const toggle = (c: Candidate) => {
    const k = keyOf(c);
    if (c.kind === "dim") setOn((s) => (s.has(k) ? new Set([...s].filter((x) => x !== k)) : new Set([...s, k])));
    else setOff((s) => (s.has(k) ? new Set([...s].filter((x) => x !== k)) : new Set([...s, k])));
  };
  if (savedId) {
    return (
      <div className="grid gap-4">
        <p className="flex items-center gap-2 text-base font-semibold text-success">
          <Check className="size-5" /> Saved. It shows in Build under "Your prep presets".
        </p>
        <DialogFooter>
          <LibraryLink search={{ tab: "prep", item: savedId }} className="font-semibold text-primary hover:underline">
            Open it in Make your own
          </LibraryLink>
          <Button className={PILL} onClick={onDone}>
            Done
          </Button>
        </DialogFooter>
      </div>
    );
  }
  return (
    <div className="grid gap-5">
      <p className="text-sm text-muted-foreground">
        {steps.length} {steps.length === 1 ? "step" : "steps"}: {steps.map((s) => s.id).join(", ")}
      </p>
      <MetadataFields value={metadata} onChange={setMetadata} titleLabel="Name the preset" titlePlaceholder="Weekly totals, then log" />
      {candidates.length ? (
        <fieldset className="grid gap-2">
          <legend className="mb-1 text-sm font-medium">Ask for these when the preset is used</legend>
          <div className="flex flex-wrap gap-1.5">
            {candidates.map((c) => (
              <ToggleChip key={keyOf(c)} pressed={isChosen(c)} onClick={() => toggle(c)}>
                {c.label}
              </ToggleChip>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">Settings you do not pick keep the values they have now.</p>
        </fieldset>
      ) : null}
      <ScopeChoice value={scope} onChange={setScope} tenantAllowed={can(grants, "create", "tenant_library")} />
      {save.error ? <ErrorNotice error={save.error} title="The preset was not saved" /> : null}
      <DialogFooter>
        <Button
          className={PILL}
          disabled={!metadata.title.trim() || !steps.length || save.isPending}
          onClick={() =>
            void save
              .save(presetFromSteps(steps, chosen, metadata), { scope })
              .then((s) => setSavedId(s.id))
              .catch(() => undefined)
          }
        >
          {save.isPending ? <Spinner /> : <Save />} Save preset
        </Button>
      </DialogFooter>
    </div>
  );
}

export function SaveStepsDialog({ open, onOpenChange, steps }: SaveStepsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide className="rounded-2xl">
        <DialogTitle className="display text-xl font-bold">Save these steps as a prep preset</DialogTitle>
        <DialogDescription>Do them once, reuse them on any dataset. Your team finds the preset in Build.</DialogDescription>
        {open ? <Body steps={steps} onDone={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}
