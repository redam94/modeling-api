/**
 * The sample project as the Start-here hub, the Guided strip and the checklist use it (v0.18
 * D20, D22): which sample to show, what its card says in each state, and the actions on it
 * (make it, try again, hide it, open it).
 *
 * The data hooks are WP-W1's (`./api`); this module adds the view rules on top. It lives in
 * the lazy `start-here-*` chunk (never in the entry, share or client bundles).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";

import { errorMessage } from "@/api/client";
import { useWorkspace } from "@/features/auth/workspace";
import { emitOnboarding } from "@/lib/onboarding-events";
import { toast } from "@/stores/toast";
import { useScope } from "@/stores/scope";

import {
  isSampleActive,
  sampleFraction,
  useOnboardingTenant,
  usePatchOnboarding,
  useSampleAction,
  useSamples,
  type SampleInfo,
  type SampleState,
} from "./api";
import { ok } from "./keys";

// ------------------------------------------------------------------------------ pure rules

/**
 * What the sample card shows:
 * - `none`: samples are off, the tenant has none, or it was retired (the card hides);
 * - `hidden`: the user hid it (a quiet "show it again" link);
 * - `offer`: not made yet, and this user may make it;
 * - `ask`: not made yet, and this user may not (viewers: an editor makes it);
 * - `working`: provisioning, ingesting or fitting;
 * - `ready`: the fit is done;
 * - `failed`: the last attempt failed (retry while attempts are left).
 */
export type SampleView = "none" | "hidden" | "offer" | "ask" | "working" | "ready" | "failed";

/** The sample to show: the first one that is not retired (hidden ones too, so they can come back). */
export function currentSample(samples: readonly SampleInfo[] | undefined): SampleInfo | null {
  return samples?.find((s) => s.state !== "retired") ?? null;
}

export function sampleView(info: SampleInfo | null | undefined): SampleView {
  if (!info || info.state === "retired") return "none";
  if (info.hidden) return "hidden";
  if (info.state == null) return info.can_install ? "offer" : "ask";
  if (isSampleActive(info)) return "working";
  if (info.state === "ready") return "ready";
  return "failed";
}

const STAGE_WORDS: Record<Exclude<SampleState, "ready" | "failed" | "retired">, string> = {
  provisioning: "Setting up the project",
  ingesting: "Loading 104 weeks of example sales",
  fitting: "Fitting the model",
};

/** The words for the stage a sample is in ("Fitting the model, 45%"). */
export function stageWords(info: Pick<SampleInfo, "state" | "progress">): string {
  if (info.state === "provisioning" || info.state === "ingesting") return STAGE_WORDS[info.state];
  if (info.state === "fitting") {
    const p = info.progress;
    return p != null && Number.isFinite(p) && p > 0 ? `${STAGE_WORDS.fitting}, ${Math.round(Math.min(1, p) * 100)}%` : STAGE_WORDS.fitting;
  }
  if (info.state === "ready") return "Ready";
  return "";
}

/** "About a minute", "About 3 minutes" (the sample's own estimate; never a promise). */
export function minutesWords(minutes: number): string {
  const m = Math.max(1, Math.round(minutes));
  return m === 1 ? "about a minute" : `about ${m} minutes`;
}

/** The Guided report of a ready sample (where "Open the sample project" goes), else `null`. */
export function sampleReportPath(info: SampleInfo | null | undefined): string | null {
  if (!info || info.state !== "ready" || !info.spec_id) return null;
  return `/guided/${encodeURIComponent(info.spec_id)}/report`;
}

export { sampleFraction };

// ------------------------------------------------------------------------------ hooks

/** The tenant's sample and its view (idle, `none`, without a signed-in tenant). */
export function useSample() {
  const tenant = useOnboardingTenant();
  const samples = useSamples({ enabled: tenant !== null });
  const info = currentSample(samples.data);
  return { tenant, info, view: tenant ? sampleView(info) : ("none" as SampleView), query: samples };
}

/** Make, retry, hide, show again or open the sample. */
export function useSampleActions() {
  const tenant = useOnboardingTenant();
  const action = useSampleAction();
  const patch = usePatchOnboarding();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const ws = useWorkspace();

  const start = useCallback(
    (info: SampleInfo) => {
      const kind = info.state === "failed" ? "retry" : "install";
      action.mutate(
        { id: info.id, action: kind },
        { onError: (e) => toast.error(kind === "retry" ? "The app could not start the sample again" : "The app could not make the sample", errorMessage(e)) },
      );
    },
    [action],
  );

  const setHidden = useCallback(
    (info: SampleInfo, hidden: boolean) => {
      patch.mutate(
        { patch: hidden ? { hide_samples: [info.id] } : { unhide_samples: [info.id] } },
        { onSettled: () => void qc.invalidateQueries({ queryKey: ok.samples(tenant ?? "-") }) },
      );
      // show the change at once; the server's list replaces the guess
      qc.setQueryData<SampleInfo[]>(ok.samples(tenant ?? "-"), (prev) => prev?.map((s) => (s.id === info.id ? { ...s, hidden } : s)));
    },
    [patch, qc, tenant],
  );

  /**
   * Switch to the sample's project and open its Guided report. A person who is not a member
   * of the sample project yet joins it first (`:install` on a made sample joins, audited).
   */
  const open = useCallback(
    (info: SampleInfo, source: string) => {
      const to = sampleReportPath(info);
      const projectId = info.project_id;
      if (!to || !projectId || !tenant) return;
      const go = () => {
        const cur = useScope.getState().active;
        if (!cur || cur.projectId !== projectId) {
          void qc.cancelQueries({ queryKey: ["p"] });
          useScope.getState().select({ tenantId: tenant, projectId });
        }
        emitOnboarding("sample_opened", { sample: info.id, source });
        void navigate({ to });
      };
      const member = ws.choices.some((c) => c.tenant.id === tenant && c.project.project_id === projectId);
      if (member || !info.can_install) {
        go();
        return;
      }
      action.mutate({ id: info.id, action: "install" }, { onSuccess: go, onError: (e) => toast.error("The app could not open the sample", errorMessage(e)) });
    },
    [action, navigate, qc, tenant, ws.choices],
  );

  return { start, setHidden, open, pending: action.isPending };
}
