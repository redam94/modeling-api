/**
 * "Save as preset" (v0.18 D4): keep a model block's settings as a team model recipe over that
 * catalog model, and choose which settings people may change when they use it. Mounted by the
 * spec editor's model panel (WP-W3); it saves straight to the library, nothing in the spec
 * changes.
 *
 * Props: `model` is the spec's `model` block (`use: <id>@<range>`, `settings`).
 */
import { Check, Save } from "lucide-react";
import { useMemo, useState } from "react";

import { can } from "@/api/access";
import { useCatalog } from "@/api/queries";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { useGrants } from "@/features/auth/workspace";
import { safeSchema } from "@/features/forms/schema-sanitize";
import type { JsonObject as FormObject } from "@/features/forms/json-schema-form";

import { useSaveDoc } from "./actions";
import { newPresetRecipe } from "./doc";
import { LibraryLink, MetadataFields, PILL, ScopeChoice, ToggleChip } from "./ui";
import { MAX_EXPOSED, splitUseRef, type JsonObject, type LibraryScope, type ModelRecipeDoc } from "./types";

export interface SavePresetDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  model: { use: string; settings?: JsonObject | null };
}

function Body({ model, onDone }: { model: SavePresetDialogProps["model"]; onDone: () => void }) {
  const grants = useGrants();
  const catalog = useCatalog();
  const [baseId, range] = splitUseRef(model.use);
  const entry = catalog.data?.models?.find((m) => m.manifest.id === baseId);
  const keys = useMemo(() => Object.keys((safeSchema((entry?.manifest.settings_schema ?? {}) as FormObject).properties as FormObject | undefined) ?? {}), [entry]);
  const [doc, setDoc] = useState<ModelRecipeDoc>(() => ({ ...newPresetRecipe(range ? model.use : `${baseId}@^1`, ""), settings: model.settings ?? {} }));
  const [scope, setScope] = useState<LibraryScope>("project");
  const [savedId, setSavedId] = useState<string | null>(null);
  const save = useSaveDoc(null);
  const exposed = doc.exposed ?? [];
  if (savedId) {
    return (
      <div className="grid gap-4">
        <p className="flex items-center gap-2 text-base font-semibold text-success">
          <Check className="size-5" /> Saved. Your team can use it from Build.
        </p>
        <DialogFooter>
          <LibraryLink search={{ tab: "models", item: savedId }} className="font-semibold text-primary hover:underline">
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
      <MetadataFields value={doc.metadata} onChange={(metadata) => setDoc({ ...doc, metadata })} titleLabel="Name the preset" titlePlaceholder={`Our usual ${baseId}`} />
      <fieldset className="grid gap-2">
        <legend className="mb-1 text-sm font-medium">What can people change when they use it? (up to {MAX_EXPOSED})</legend>
        <div className="flex flex-wrap gap-1.5">
          {keys.map((k) => {
            const on = exposed.includes(k);
            return (
              <ToggleChip key={k} pressed={on} disabled={!on && exposed.length >= MAX_EXPOSED} onClick={() => setDoc({ ...doc, exposed: on ? exposed.filter((x) => x !== k) : [...exposed, k] })}>
                <span className="font-mono">{k}</span>
              </ToggleChip>
            );
          })}
          {!keys.length ? <p className="text-sm text-muted-foreground">{catalog.isPending ? "Loading the model's settings…" : "This model has no settings to share."}</p> : null}
        </div>
      </fieldset>
      <ScopeChoice value={scope} onChange={setScope} tenantAllowed={can(grants, "create", "tenant_library")} />
      {save.error ? <ErrorNotice error={save.error} title="The preset was not saved" /> : null}
      <DialogFooter>
        <Button
          className={PILL}
          disabled={!doc.metadata.title.trim() || save.isPending}
          onClick={() =>
            void save
              .save(doc, { scope })
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

export function SavePresetDialog({ open, onOpenChange, model }: SavePresetDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide className="rounded-2xl">
        <DialogTitle className="display text-xl font-bold">Save these settings as a preset</DialogTitle>
        <DialogDescription>Your team gets {model.use} with your settings, ready to use. Pick what they may change.</DialogDescription>
        {open ? <Body model={model} onDone={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}
