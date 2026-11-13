/**
 * A team workflow template opened from Make your own: its name and description, and the
 * workflow document as YAML. Templates are made in the workflow editor ("Save as team
 * template"); here they can be renamed, adjusted and started from. An invalid workflow document
 * is refused on save (unlike the other kinds, D8), so the problems show before anything is kept.
 */
import { Play, Save } from "lucide-react";
import { useMemo, useState } from "react";

import { can } from "@/api/access";
import { ErrorNotice, IssueList, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { useGrants } from "@/features/auth/workspace";
import { cn } from "@/lib/utils";
import { useDebounced } from "@/hooks/use-debounced";

import { useSaveDoc, useStartFromTemplate } from "./actions";
import { docKey, isStaleRevision, useLibraryItem, useLibraryValidation } from "./api";
import { tidyDoc } from "./doc";
import { BuilderSection, MetadataFields, PILL, PILL_SM } from "./ui";
import { LazyYamlTab as YamlTab } from "./yaml-tab-lazy";
import { isWorkflowTemplateDoc, type JsonObject, type LibraryDoc, type WorkflowTemplateDoc } from "./types";

export function WorkflowTemplateEditor({ itemId, initial }: { itemId: string; initial: WorkflowTemplateDoc }) {
  const grants = useGrants();
  const item = useLibraryItem(itemId);
  const [doc, setDoc] = useState(initial);
  const [savedKey, setSavedKey] = useState(docKey(tidyDoc(initial)));
  const save = useSaveDoc(item.data);
  const start = useStartFromTemplate();
  const tidy = useMemo(() => tidyDoc(doc), [doc]);
  const validation = useLibraryValidation(useDebounced(tidy, 300) as unknown as JsonObject);
  const issues = validation.data?.issues;
  const dirty = docKey(tidy) !== savedKey;
  const canEdit = item.data ? (item.data.scope === "tenant" ? can(grants, "update", "tenant_library") : can(grants, "update", "library")) : false;
  return (
    <div className="grid gap-5">
      <BuilderSection n={1} title="About this template" hint="What your teammates see in the workflow gallery under From your team.">
        <MetadataFields value={doc.metadata} onChange={(metadata) => setDoc({ ...doc, metadata })} issues={issues} locked />
      </BuilderSection>
      <BuilderSection n={2} title="The workflow" hint="Edit the workflow as YAML. The check runs as you type.">
        <YamlTab kind="workflow_template" doc={doc} issues={issues} onDoc={(d: LibraryDoc) => isWorkflowTemplateDoc(d) && setDoc(d)} />
      </BuilderSection>
      {issues?.length ? (
        <section aria-label="What the check found" className="rounded-2xl border bg-card p-4 [overflow-wrap:anywhere]">
          <h2 className="mb-2 text-base font-semibold">What the check found</h2>
          <IssueList issues={issues} />
        </section>
      ) : null}
      <div className="flex flex-wrap items-center gap-3 rounded-3xl bg-block p-5 text-block-foreground">
        {canEdit ? (
          <Button
            className={PILL}
            disabled={!dirty || save.isPending}
            onClick={() =>
              void save
                .save(doc)
                .then(() => setSavedKey(docKey(tidy)))
                .catch((e: unknown) => {
                  if (isStaleRevision(e)) void item.refetch();
                })
            }
          >
            {save.isPending ? <Spinner /> : <Save />} {dirty ? "Save a new version" : "Saved"}
          </Button>
        ) : null}
        {item.data ? (
          <Button variant="ghost" className={cn(PILL_SM, "text-block-foreground shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised hover:text-block-foreground")} disabled={start.busy !== null || dirty} onClick={() => item.data && void start.start(item.data)}>
            {start.busy ? <Spinner /> : <Play />} Start a workflow from it
          </Button>
        ) : null}
        <p className="text-xs text-block-muted">{dirty ? "Save your changes before you start from it." : "Starting makes a new workflow in this project. The template stays as it is."}</p>
      </div>
      {save.error ? <ErrorNotice error={save.error} title={isStaleRevision(save.error) ? "Someone saved this item after you opened it" : "The template was not saved"} /> : null}
      {start.error ? <ErrorNotice error={start.error} title="The workflow was not made" /> : null}
    </div>
  );
}
