/**
 * The builder's working copy of a dashboard draft. Setup and Inputs edit it locally; "Save
 * draft" sends it with the `draft_etag` it was read with (`PUT …/draft`, `If-Match`). A
 * newer draft from the server replaces the copy only when there are no unsaved changes; with
 * changes, a save answers 412 `DASHBOARD_SPEC_STALE` and the page offers to reload.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "@/api/client";

import { useRefetchDashboard, useUpdateDraft } from "../api";
import { type Dashboard, type DashboardSpec, layout1Spec } from "../types";
import { cleanSpec, sameSpec } from "./spec-form";

export interface Draft {
  spec: DashboardSpec;
  /** The etag the working copy is based on. */
  etag: string;
  dirty: boolean;
  /** The server's draft moved since this copy was read (a save would be refused). */
  stale: boolean;
  saving: boolean;
  saveError: unknown;
  set: (next: DashboardSpec | ((s: DashboardSpec) => DashboardSpec)) => void;
  save: () => Promise<Dashboard | null>;
  /** Drop the changes and take the server's draft (read again first: the cached copy may be the old one). */
  reset: () => Promise<void>;
}

export function useDraft(d: Dashboard): Draft {
  const update = useUpdateDraft(d.id);
  const refetch = useRefetchDashboard(d.id);
  const [spec, setSpec] = useState<DashboardSpec>(layout1Spec(d.draft_spec));
  // the server draft the working copy started from, and its etag
  const [base, setBase] = useState<{ spec: DashboardSpec; etag: string }>({ spec: layout1Spec(d.draft_spec), etag: d.draft_etag });
  const [stale, setStale] = useState(false);
  const dirty = !sameSpec(spec, base.spec);

  useEffect(() => {
    if (d.draft_etag === base.etag) return;
    if (!dirty || sameSpec(spec, layout1Spec(d.draft_spec))) {
      setSpec(layout1Spec(d.draft_spec));
      setBase({ spec: layout1Spec(d.draft_spec), etag: d.draft_etag });
      setStale(false);
    } else {
      setStale(true);
    }
    // only a new server draft triggers this
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d.draft_etag]);

  const set = useCallback((next: DashboardSpec | ((s: DashboardSpec) => DashboardSpec)) => setSpec((s) => (typeof next === "function" ? next(s) : next)), []);

  const save = async (): Promise<Dashboard | null> => {
    try {
      const out = await update.mutateAsync({ spec: cleanSpec(spec), etag: base.etag });
      setSpec(layout1Spec(out.draft_spec));
      setBase({ spec: layout1Spec(out.draft_spec), etag: out.draft_etag });
      setStale(false);
      return out;
    } catch (e) {
      if (e instanceof ApiError && (e.status === 412 || e.code === "DASHBOARD_SPEC_STALE")) setStale(true);
      return null;
    }
  };

  const reset = async () => {
    const fresh = (await refetch()) ?? d;
    setSpec(layout1Spec(fresh.draft_spec));
    setBase({ spec: layout1Spec(fresh.draft_spec), etag: fresh.draft_etag });
    setStale(false);
    update.reset();
  };

  return { spec, etag: base.etag, dirty, stale, saving: update.isPending, saveError: update.error, set, save, reset };
}
