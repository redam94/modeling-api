/**
 * `/choose`: "Help me choose" (M3.5). The answers live in the URL
 * (`?goal=forecast&data_shape=one_series`), so a suggestion can be shared or bookmarked.
 */
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import { useMemo } from "react";

import { PageHeader } from "@/components/common";
import { SegmentedControl } from "@/components/ui/overlays";
import { HelpMeChoose } from "@/features/about/chooser";
import type { ChooserAnswers, ChooserKind } from "@/features/about/types";

const route = getRouteApi("/choose");

const KINDS: { value: "start" | ChooserKind; label: string }[] = [
  { value: "start", label: "Where to start" },
  { value: "model", label: "Models" },
  { value: "step", label: "Preparation steps" },
  { value: "check", label: "Governance checks" },
  { value: "node", label: "Workflow nodes" },
];

/** Search parameters → answers (`goal=a,b` → `{goal: ["a", "b"]}`), without `kind`. */
export function answersFromSearch(search: Record<string, string | undefined>): ChooserAnswers {
  const out: ChooserAnswers = {};
  for (const [k, v] of Object.entries(search)) {
    if (k === "kind" || !v) continue;
    out[k] = v.split(",").filter(Boolean);
  }
  return out;
}

export function searchFromAnswers(answers: ChooserAnswers, kind: string | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, values] of Object.entries(answers)) if (values.length) out[k] = values.join(",");
  if (kind) out.kind = kind;
  return out;
}

export function ChoosePage() {
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/choose" });
  const answers = useMemo(() => answersFromSearch(search), [search]);
  const kind = KINDS.some((k) => k.value === search.kind) && search.kind !== "start" ? (search.kind as ChooserKind) : null;
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-6">
      <PageHeader
        title="Help me choose"
        description="Answer some questions about what you want to know and about your data. The page shows the best models, analyses and workflows for you, with the reasons. Click one to start it."
        actions={<SegmentedControl label="What to suggest" value={kind ?? "start"} options={KINDS} onValueChange={(v) => void navigate({ search: searchFromAnswers(answers, v === "start" ? undefined : v), replace: true })} />}
      />
      <HelpMeChoose answers={answers} onAnswersChange={(a) => void navigate({ search: searchFromAnswers(a, kind ?? undefined), replace: true })} kind={kind} limit={6} layout="page" />
    </div>
  );
}
