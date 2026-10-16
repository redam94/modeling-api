/**
 * Everything a saved Guided session is made of, looked up from the server (see `session.ts`):
 * the spec and its revisions, the template, the dataset and its newest version, the fit runs and
 * the model version the latest successful fit produced.
 */
import { useMemo } from "react";

import { useSpec, useSpecRevision, useSpecRevisions } from "@/api/queries";
import type { DatasetVersion, Dataset, ModelVersion, Run, SpecDoc, SpecRevision } from "@/api/types";

import { useDatasetByName, useFitRuns, useGuidedTemplate, useLatestDatasetVersion, useModelVersionForRun } from "./api";
import { advancedChanges, datasetNameOf, GUIDED_MESSAGE, guidedTemplateOf, specName } from "./session";
import type { GuidedTemplate, SpecDocument } from "./types";

export interface GuidedSession {
  spec: SpecDoc;
  latest: SpecRevision;
  document: SpecDocument;
  template: GuidedTemplate;
  dataset: Dataset | null;
  datasetVersion: DatasetVersion | null;
  fits: Run[];
  /** The newest fit of the newest revision (what the run step shows). */
  currentFit: Run | null;
  /** The newest successful fit (whatever revision it fitted). */
  lastGoodFit: Run | null;
  modelVersion: ModelVersion | null;
  /** Advanced or hidden settings changed in Expert mode since Guided mode drafted the spec. */
  advanced: string[];
}

export interface SessionState {
  session: GuidedSession | null;
  isPending: boolean;
  error: unknown;
  /** The spec is not a Guided spec (no `metadata.labels.guided`). */
  notGuided: boolean;
}

export function useGuidedSession(specId: string): SessionState {
  const spec = useSpec(specId);
  const latestRev = spec.data?.latest_revision;
  const revision = useSpecRevision(specId, latestRev);
  const revisions = useSpecRevisions(specId);
  const document = (revision.data?.document ?? spec.data?.latest?.document ?? null);
  const templateId = guidedTemplateOf(document);
  const template = useGuidedTemplate(templateId);
  const dataset = useDatasetByName(datasetNameOf(document));
  const version = useLatestDatasetVersion(dataset.data?.id);
  const fits = useFitRuns(specId);
  const runs = useMemo(() => fits.data ?? [], [fits.data]);
  const lastGoodFit = runs.find((r) => r.state === "succeeded" && r.output_model_version_id) ?? null;
  const mv = useModelVersionForRun(specName(document), lastGoodFit);

  // The first revision Guided mode wrote is the reference for "advanced settings applied".
  const firstGuided = useMemo(() => {
    const items = [...(revisions.data?.items ?? [])].sort((a, b) => a.revision - b.revision);
    return items.find((r) => (r.message ?? "").startsWith(GUIDED_MESSAGE)) ?? null;
  }, [revisions.data]);

  const latest = revision.data ?? spec.data?.latest ?? null;
  const error = spec.error ?? revision.error ?? template.error ?? null;
  const pending = spec.isPending || (!latest && !error) || (Boolean(templateId) && template.isPending);
  if (!spec.data || !latest || !document) return { session: null, isPending: pending, error, notGuided: false };
  if (!templateId) return { session: null, isPending: false, error: null, notGuided: true };
  if (!template.data) return { session: null, isPending: pending, error, notGuided: false };
  const currentFit = runs.find((r) => r.spec_revision === latest.revision) ?? null;
  return {
    session: {
      spec: spec.data,
      latest,
      document,
      template: template.data,
      dataset: dataset.data ?? null,
      datasetVersion: version.data ?? null,
      fits: runs,
      currentFit,
      lastGoodFit,
      modelVersion: mv.data ?? null,
      advanced: firstGuided ? advancedChanges(template.data, document, firstGuided.document) : [],
    },
    isPending: false,
    error: null,
    notGuided: false,
  };
}
