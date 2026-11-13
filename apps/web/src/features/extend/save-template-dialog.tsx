/**
 * "Save as team template" (v0.18 D8): keep the workflow open in the editor as a team workflow
 * template in the library. The server validates the document with the merged node contracts
 * and refuses an invalid one (422, unlike other library kinds), so the dialog shows those
 * problems and nothing is saved. Project scope by default; "Everyone in the tenant" only for
 * tenant admins. People then start from it in the gallery ("From your team").
 *
 * Lazy-loaded by the workflow editor (default export).
 */
import { Check, Save } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { useGrants } from "@/features/auth/workspace";
import { useSaveDoc } from "@/features/library/actions";
import { newWorkflowTemplate } from "@/features/library/doc";
import type { JsonObject, LibraryMetadata, LibraryScope } from "@/features/library/types";
import { LibraryLink, MetadataFields, PILL, ScopeChoice } from "@/features/library/ui";

export interface SaveTemplateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The workflow document as the editor holds it. */
  document: JsonObject;
  /** A starting title (the workflow's name). */
  title?: string;
}

/**
 * The template's copy of a workflow document: the same document without the lineage labels it
 * was made from (a gallery template or a library item): a team template starts its own (D9).
 * `metadata.project` stays (the document schema needs it; the use path names the project).
 */
export function templateDocument(document: JsonObject): JsonObject {
  const doc = structuredClone(document);
  const md = doc.metadata;
  if (md && typeof md === "object" && !Array.isArray(md)) {
    const labels = md.labels;
    if (labels && typeof labels === "object" && !Array.isArray(labels)) {
      delete labels["workflow-template"];
      for (const k of Object.keys(labels)) if (k.startsWith("mapi.library/")) delete labels[k];
      if (!Object.keys(labels).length) delete md.labels;
    }
  }
  return doc;
}

function Body({ document, title, onDone }: { document: JsonObject; title: string; onDone: () => void }) {
  const grants = useGrants();
  const [metadata, setMetadata] = useState<LibraryMetadata>(() => newWorkflowTemplate({}, title).metadata);
  const [scope, setScope] = useState<LibraryScope>("project");
  const [savedId, setSavedId] = useState<string | null>(null);
  const save = useSaveDoc(null);
  if (savedId) {
    return (
      <div className="grid gap-4" role="status">
        <p className="flex items-center gap-2 text-base font-semibold text-success">
          <Check aria-hidden className="size-5" /> Saved. Your team finds it in the gallery under "From your team".
        </p>
        <DialogFooter>
          <LibraryLink search={{ tab: "workflows", item: savedId }} className="link font-semibold">
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
      <MetadataFields value={metadata} onChange={setMetadata} titleLabel="Name the template" titlePlaceholder="Weekly readiness check" />
      <ScopeChoice value={scope} onChange={setScope} tenantAllowed={can(grants, "create", "tenant_library")} />
      {save.error ? <ErrorNotice error={save.error} title="The template was not saved" /> : null}
      <DialogFooter>
        <Button variant="ghost" className={PILL} onClick={onDone}>
          Cancel
        </Button>
        <Button
          className={PILL}
          disabled={!metadata.title.trim() || save.isPending}
          onClick={() =>
            void save
              .save({ ...newWorkflowTemplate(templateDocument(document)), metadata }, { scope })
              .then((s) => setSavedId(s.id))
              .catch(() => undefined)
          }
        >
          {save.isPending ? <Spinner /> : <Save />} Save template
        </Button>
      </DialogFooter>
    </div>
  );
}

export function SaveTemplateDialog({ open, onOpenChange, document, title = "" }: SaveTemplateDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide className="rounded-2xl">
        <DialogTitle className="display text-xl font-semibold">Save as a team template</DialogTitle>
        <DialogDescription>Turn this workflow into a starting point. Your team picks it in the gallery and makes their own copy.</DialogDescription>
        {open ? <Body document={document} title={title} onDone={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}

export default SaveTemplateDialog;
