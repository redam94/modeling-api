/**
 * A dataset's About (M3.5): the owner's own description (editable) next to the summary the
 * platform generates from the latest version's schema and coordinates
 * (`GET`/`PATCH /v1/datasets/{dataset_id}/about`).
 */
import { AlertTriangle, Pencil, Sparkles } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { errorMessage } from "@/api/client";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Textarea } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { toast } from "@/stores/toast";

import { useDatasetAbout, useUpdateDatasetAbout } from "./api";

export const DESCRIPTION_MAX = 4000;

export function DatasetAboutCard({ datasetId, actions }: { datasetId: string; actions?: React.ReactNode }) {
  const about = useDatasetAbout(datasetId);
  const update = useUpdateDatasetAbout(datasetId);
  const grants = useGrants();
  const editable = can(grants, "update", "dataset");
  const [draft, setDraft] = useState<string | null>(null);
  const editing = draft !== null;
  if (about.isPending) return <LoadingRows rows={2} />;
  if (about.isError) return <ErrorNotice error={about.error} title="The dataset's About could not be loaded" />;
  const a = about.data;
  const save = () => {
    if (draft === null) return;
    const text = draft.trim();
    update.mutate(
      { description_user: text || null },
      {
        onSuccess: () => {
          setDraft(null);
          toast.success("Description saved");
        },
        onError: (e) => toast.error("The description was not saved", errorMessage(e)),
      },
    );
  };
  return (
    <Card aria-labelledby="dataset-about-h">
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle id="dataset-about-h">About this dataset</CardTitle>
          <CardDescription>What it holds, in plain words: the owner&apos;s description and a summary made from the data.</CardDescription>
        </div>
        <div className="flex flex-wrap gap-2">{actions}</div>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-2">
        <section className="grid content-start gap-2" aria-labelledby="ds-desc-h">
          <div className="flex items-center justify-between gap-2">
            <h3 id="ds-desc-h" className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Description
            </h3>
            {editable && !editing ? (
              <Button variant="ghost" size="sm" onClick={() => setDraft(a.description_user ?? "")}>
                <Pencil /> {a.description_user ? "Edit" : "Add a description"}
              </Button>
            ) : null}
          </div>
          {editing ? (
            <form
              className="grid gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                save();
              }}
            >
              <Field
                label="Describe the data for your colleagues"
                htmlFor="ds-about-text"
                hint={`Where it comes from, how often it is refreshed, what to watch out for. ${DESCRIPTION_MAX - (draft?.length ?? 0)} characters left.`}
                error={(draft?.length ?? 0) > DESCRIPTION_MAX ? "That is too long." : undefined}
              >
                <Textarea id="ds-about-text" value={draft ?? ""} onChange={(e) => setDraft(e.target.value)} rows={4} autoFocus />
              </Field>
              <div className="flex gap-2">
                <Button type="submit" size="sm" disabled={update.isPending || (draft?.length ?? 0) > DESCRIPTION_MAX}>
                  Save description
                </Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setDraft(null)}>
                  Cancel
                </Button>
              </div>
            </form>
          ) : a.description_user ? (
            <p className="text-[14px] leading-relaxed whitespace-pre-line" data-testid="dataset-description">
              {a.description_user}
            </p>
          ) : (
            <p className="text-[13px] text-muted-foreground">No description yet.{editable ? " Add one so others know where the data comes from." : ""}</p>
          )}
        </section>
        <section className="grid content-start gap-2" aria-labelledby="ds-summary-h">
          <h3 id="ds-summary-h" className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            <Sparkles aria-hidden className="size-3.5" /> Summary from the data
          </h3>
          <p className="text-[14px] leading-relaxed" data-testid="dataset-summary">
            {a.summary_generated}
          </p>
          {a.highlights?.length ? (
            <ul className="flex flex-wrap gap-1.5" aria-label="Highlights">
              {a.highlights.map((h) => (
                <li key={h}>
                  <Badge variant="secondary">{h}</Badge>
                </li>
              ))}
            </ul>
          ) : null}
          {a.warnings_plain?.length ? (
            <ul className="grid gap-1 text-[13px]" aria-label="Things to check">
              {a.warnings_plain.map((w) => (
                <li key={w} className="flex items-start gap-1.5">
                  <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" />
                  {w}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      </CardContent>
    </Card>
  );
}
