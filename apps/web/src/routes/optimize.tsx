/**
 * `/models/$modelId/versions/$version/optimize`: budget optimisation on a marketing-mix model
 * (M3.5, mmm-framework planning). The form needs the channels and today's spend, read from
 * the fit's response grid (or its response-curve view); the plan is an `optimize` run.
 * `?run=` keeps the plan in the URL; `?back=` returns to the Guided analysis it came from.
 */
import { useQuery } from "@tanstack/react-query";
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Wallet } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { can } from "@/api/access";
import { errorMessage } from "@/api/client";
import { modelArtifactLink, useModel, useModelVersion, useProjectScope } from "@/api/queries";
import { EmptyState, ErrorNotice, LoadingRows, Mono, PageHeader, ProblemNotice } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { RunBanner } from "@/features/explore/explorer-shell";
import { parseView, viewName } from "@/features/explore/views";
import { useResponseGrid } from "@/features/guided/api";
import { useEffectivePersona } from "@/features/guided/persona";
import { useStartOptimize, useOptimization } from "@/features/optimize/api";
import { PlanFormView } from "@/features/optimize/form";
import { defaultForm, toRequest, type PlanForm } from "@/features/optimize/plan";
import { PlanResult } from "@/features/optimize/result";
import { fetchArtifactJson } from "@/lib/artifacts";
import { toast } from "@/stores/toast";

const route = getRouteApi("/models/$modelId/versions/$version/optimize");

interface Channels {
  channels: string[];
  current: Record<string, number>;
  geos: string[];
  units: string | null;
  periods: number | null;
}

/** Channels and today's spend: the response grid, else the response-curve explorer view. */
function useChannels(modelId: string, version: number, mv: Parameters<typeof useResponseGrid>[0]) {
  const grid = useResponseGrid(mv);
  const scope = useProjectScope();
  const curves = useQuery({
    queryKey: ["p", scope.key, "models", modelId, "versions", version, "optimize-channels"],
    enabled: grid.isSuccess && !grid.data,
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      try {
        const link = await modelArtifactLink(modelId, version, viewName("response_curves"));
        return parseView("response_curves", await fetchArtifactJson(link, { signal }));
      } catch {
        return null;
      }
    },
  });
  return useMemo((): { data: Channels | null; isPending: boolean } => {
    if (grid.isPending || (grid.isSuccess && !grid.data && curves.isPending)) return { data: null, isPending: true };
    if (grid.data) {
      const g = grid.data;
      return { data: { channels: [...g.channels], current: Object.fromEntries(g.channels.map((c, i) => [c, g.current_spend[i] ?? 0])), geos: [...(g.geos ?? [])], units: g.spend_units ?? null, periods: g.periods ?? null }, isPending: false };
    }
    const v = curves.data;
    if (v) return { data: { channels: [...v.channels], current: Object.fromEntries(v.channels.map((c, i) => [c, v.current_spend?.[i] ?? 0])), geos: [], units: null, periods: null }, isPending: false };
    return { data: null, isPending: false };
  }, [grid.isPending, grid.isSuccess, grid.data, curves.isPending, curves.data]);
}

export function OptimizePage() {
  const { modelId, version } = route.useParams();
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/models/$modelId/versions/$version/optimize" });
  const v = Number(version);
  const valid = Number.isInteger(v) && v > 0;
  const model = useModel(modelId);
  const mv = useModelVersion(modelId, valid ? v : undefined);
  const expert = useEffectivePersona() === "expert";
  const channels = useChannels(modelId, v, mv.data);
  const start = useStartOptimize(modelId, v);
  const optimization = useOptimization(search.run ?? null);
  const grants = useGrants();
  const [form, setForm] = useState<PlanForm | null>(null);
  useEffect(() => {
    if (!form && channels.data) setForm(defaultForm(channels.data.channels));
  }, [form, channels.data]);

  if (!valid) return <ErrorNotice error={new Error("The version must be a positive integer.")} />;
  if (mv.isError) return <ErrorNotice error={mv.error} title="The app could not load this model version" />;
  const back = search.back ? (
    <Button asChild variant="ghost">
      <Link to="/guided/$specId/$step" params={{ specId: search.back, step: "explore" }}>
        <ArrowLeft /> Back to your analysis
      </Link>
    </Button>
  ) : (
    <Button asChild variant="ghost">
      <Link to="/models/$modelId/versions/$version" params={{ modelId, version: String(v) }}>
        <ArrowLeft /> Back to the version
      </Link>
    </Button>
  );
  const header = (
    <PageHeader
      title="Optimise a budget"
      description={expert ? "Divide the budget across channels (and markets) to get the maximum result. The plan uses all the plausible versions of the model. It has bounds, groups, a cautious objective and the efficient frontier." : "Find the best division of your marketing budget from what the model learned. The plan includes the uncertainty of the model."}
      meta={
        <>
          {model.data ? <span className="font-medium text-foreground">{model.data.name}</span> : null}
          {mv.data ? <Mono>{mv.data.ref}</Mono> : null}
          <Badge variant="outline">mmm-framework planning</Badge>
        </>
      }
      actions={back}
    />
  );
  if (mv.isPending || channels.isPending) {
    return (
      <div className="grid gap-6">
        {header}
        <LoadingRows rows={6} />
      </div>
    );
  }
  if (!mv.data.capabilities?.decision) {
    return (
      <div className="grid gap-6">
        {header}
        <EmptyState icon={<Wallet />} title="This model does not optimise budgets">
          A budget plan needs a marketing-mix model (mmm.bayesian or mmm.frequentist). The model must know how each channel responds to spend.
        </EmptyState>
      </div>
    );
  }
  if (!channels.data || !form) {
    return (
      <div className="grid gap-6">
        {header}
        <EmptyState icon={<Wallet />} title="No response curves for this version">
          The fit did not keep the response curves. Thus the app does not know the channels and the current spend. Fit the model again with <Mono>outputs.derived: [response_curves]</Mono>.
        </EmptyState>
      </div>
    );
  }
  const submit = () =>
    start.mutate(toRequest(form), {
      onSuccess: (o) => void navigate({ search: (prev) => ({ ...prev, run: o.run.id }) }),
      onError: (e) => toast.error("The optimisation did not start", errorMessage(e)),
    });
  const o = optimization.data;
  return (
    <div className="grid gap-6">
      {header}
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 2xl:grid-cols-[minmax(0,34rem)_minmax(0,1fr)]">
        <PlanFormView
          form={form}
          onChange={setForm}
          channels={channels.data.channels}
          current={channels.data.current}
          geos={channels.data.geos}
          units={channels.data.units}
          periods={channels.data.periods}
          expert={expert}
          onSubmit={submit}
          pending={start.isPending || Boolean(o && !o.done)}
          canRun={can(grants, "run", "model")}
        />
        <section aria-label="The plan" className="grid content-start gap-4">
          {o && !o.done ? <RunBanner exploration={o} title="Optimising the budget" /> : null}
          {optimization.isError ? <ErrorNotice error={optimization.error} title="The app could not load the plan" /> : null}
          {o?.done && o.run.problem ? <ProblemNotice problem={o.run.problem} title="The optimisation of the budget failed" /> : null}
          {o?.done && o.result ? <PlanResult result={o.result} expert={expert} /> : null}
          {!o && !search.run ? (
            <EmptyState icon={<Wallet />} title="Your plan appears here">
              Select the budget and the level of caution. Then start the optimisation. You can change the limits and run it again. The run history keeps each plan.
            </EmptyState>
          ) : null}
        </section>
      </div>
    </div>
  );
}
