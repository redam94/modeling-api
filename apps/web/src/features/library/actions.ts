/**
 * What the builders do with a document: save it (a new item, or a new revision with `If-Match`)
 * and use it. A used model recipe becomes a normal spec in Build (with its lineage label, D9),
 * and "Fit now" also starts the fit; a used workflow template becomes a workflow.
 */
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { api, ApiError, newIdempotencyKey, unwrap } from "@/api/client";
import { useCreateRun, useCreateSpec, useProjectScope, type ProjectScope } from "@/api/queries";
import { useCreateWorkflow } from "@/features/workflows/api";

import { useCreateLibraryItem, useReviseLibraryItem } from "./api";
import { specFromFragment, specNameFor, specYaml, tidyDoc } from "./doc";
import type { JsonObject, LibraryCreate, LibraryDoc, LibraryItem, LibraryScope, LibraryUseRequest, LibraryUseResult } from "./types";

export interface Saved {
  id: string;
  revision: number;
  valid: boolean;
}

/** Save `doc` as a new item, or as a new revision of `item` (412 when someone saved in between). */
export function useSaveDoc(item: LibraryItem | null | undefined) {
  const create = useCreateLibraryItem();
  const revise = useReviseLibraryItem(item?.id ?? "");
  const save = async (doc: LibraryDoc, opts: { scope?: LibraryScope; message?: string | null } = {}): Promise<Saved> => {
    const document = tidyDoc(doc) as unknown as LibraryCreate["document"];
    if (item) {
      const rev = await revise.mutateAsync({ body: { document, message: opts.message ?? null }, etag: item.etag });
      return { id: item.id, revision: rev.revision, valid: rev.valid };
    }
    const created = await create.mutateAsync({ scope: opts.scope ?? "project", document, message: opts.message ?? null });
    return { id: created.id, revision: created.latest_revision, valid: true };
  };
  return { save, isPending: create.isPending || revise.isPending, error: item ? revise.error : create.error, reset: () => (item ? revise.reset() : create.reset()) };
}

/** `POST /v1/library/{id}:use` for any item. */
export async function requestItemUse(s: ProjectScope, id: string, body: LibraryUseRequest): Promise<LibraryUseResult> {
  return (await unwrap(api.POST("/v1/library/{item_id}:use", { params: { path: { item_id: id } }, headers: s.headers, body })));
}

export type UseTarget = "build" | "fit";

/**
 * Turn a saved model recipe into a spec on `dataset` (and start a fit with `target: "fit"`),
 * then open it. `exposed` holds the values of a preset's exposed settings.
 */
export function useModelRecipeUse() {
  const s = useProjectScope();
  const createSpec = useCreateSpec();
  const createRun = useCreateRun();
  const navigate = useNavigate();
  const [busy, setBusy] = useState<UseTarget | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async (opts: { itemId: string; revision: number; name: string; dataset: string; target: UseTarget; exposed?: JsonObject }) => {
    setBusy(opts.target);
    setError(null);
    try {
      const used = await requestItemUse(s, opts.itemId, { revision: opts.revision, dataset: opts.dataset, exposed: opts.exposed ?? {}, slots: {} });
      const spec = specFromFragment((used.spec_fragment ?? {}) as JsonObject, { name: specNameFor(opts.name), project: s.projectId || "default", dataset: opts.dataset });
      const created = await createSpec.mutateAsync({ yaml: await specYaml(spec), message: `From the model recipe ${opts.name}` });
      if (opts.target === "fit") {
        const runRow = await createRun.mutateAsync({ body: { kind: "fit", spec_id: created.id, revision: created.latest_revision, labels: used.labels ?? {} }, key: newIdempotencyKey() });
        await navigate({ to: "/runs/$runId", params: { runId: runRow.id } });
      } else {
        await navigate({ to: "/build/$specId", params: { specId: created.id } });
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  return { run, busy, error, clearError: () => setError(null) };
}

/** Start a workflow from a team workflow template (D8): `:use`, then `POST /v1/workflows {document}`. */
export function useStartFromTemplate() {
  const s = useProjectScope();
  const createWorkflow = useCreateWorkflow();
  const navigate = useNavigate();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const start = async (item: LibraryItem) => {
    setBusy(item.id);
    setError(null);
    try {
      const used = await requestItemUse(s, item.id, { slots: {}, exposed: {} });
      const doc = (used.workflow_document ?? {}) as JsonObject;
      const md = (typeof doc.metadata === "object" && doc.metadata !== null && !Array.isArray(doc.metadata) ? doc.metadata : {});
      const baseName = typeof md.name === "string" && md.name ? md.name : item.name;
      // A name already taken in the project is a 409: try a few numbered names.
      let lastError: unknown = null;
      for (let n = 1; n <= 5; n++) {
        const name = n === 1 ? baseName : `${baseName}-${n}`.slice(0, 64);
        try {
          const wf = await createWorkflow.mutateAsync({ document: { ...doc, metadata: { ...md, name } } });
          await navigate({ to: "/workflows/$workflowId", params: { workflowId: (wf as { id: string }).id } });
          return;
        } catch (e) {
          lastError = e;
          if (!(e instanceof ApiError && e.status === 409)) break;
        }
      }
      throw lastError;
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };
  return { start, busy, error };
}
