import { useEffect, useMemo, useRef } from "react";

import { useCatalog, useDatasets, useDatasetVersion } from "@/api/queries";
import type { StageSchema } from "@/api/types";
import { isTree } from "@/features/datasets/tree";

import { setSpecSchema } from "./monaco-setup";
import { enrichSpecSchema, hintKey, parseDatasetRef, specShape, type HintSources, type SpecShape } from "./spec-hints";

const NO_STAGES: StageSchema[] = [];

/**
 * Everything the editor can hint from: the catalog, datasets, the columns of the dataset the
 * spec reads and the last validation's stage schemas. While the YAML does not parse (mid
 * edit) the last good shape is kept, so completions do not flicker away.
 */
export function useHintSources(document: unknown, stages: StageSchema[] | undefined): { shape: SpecShape; sources: HintSources } {
  const catalog = useCatalog();
  const datasets = useDatasets();
  const lastShape = useRef<SpecShape | null>(null);
  const shape = useMemo(() => {
    if (document === null && lastShape.current) return lastShape.current;
    return (lastShape.current = specShape(document));
  }, [document]);

  const target = shape.datasetRef ? parseDatasetRef(shape.datasetRef) : null;
  const dataset = target ? datasets.data?.items.find((d) => d.ref === target.ref) : undefined;
  const version = dataset ? (target?.version ?? dataset.latest_version ?? undefined) : undefined;
  const columns = useDatasetVersion(dataset?.id ?? "", version);

  const sources = useMemo<HintSources>(
    () => ({
      catalog: catalog.data ?? null,
      datasets: datasets.data?.items ?? [],
      columns: columns.data?.data_schema ?? null,
      stages: stages ?? NO_STAGES,
      // M4: the nodes of a tree version (data.nodes hints).
      tree: isTree(columns.data) ? columns.data.tree_schema.nodes : null,
    }),
    [catalog.data, datasets.data, columns.data, stages],
  );
  return { shape, sources };
}

/** Bind the enriched ModelSpec schema to the editor, re-binding only when a hint input changes. */
export function useSchemaHints(uri: string, base: unknown, shape: SpecShape, sources: HintSources): void {
  const key = useMemo(() => hintKey(shape, sources), [shape, sources]);
  const latest = useRef({ shape, sources });
  latest.current = { shape, sources };
  useEffect(() => {
    if (!base) return;
    const { shape: s, sources: src } = latest.current;
    void setSpecSchema(uri, enrichSpecSchema(base, s, src));
  }, [uri, base, key]);
}
