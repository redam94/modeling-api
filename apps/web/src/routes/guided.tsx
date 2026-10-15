import { Link } from "@tanstack/react-router";

import { PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { useUi } from "@/stores/ui";

const TASKS = [
  { title: "Choose what you want to learn", body: "Start from a template: geo marketing mix, weekly forecast, demand by store or customer choice." },
  { title: "Answer questions about your data", body: "Point at the revenue and spend columns; the mapping is filled in for you." },
  { title: "Enter what you already believe", body: "ROI ranges or contribution shares per channel, plus lift-test results." },
  { title: "Run and follow progress", body: "Plain-language phases with an estimate of the time left." },
  { title: "Read the report and scorecard", body: "Every check explains what it means and what to do next." },
  { title: "Try what-if scenarios", body: "Sliders over precomputed response curves that keep the uncertainty honest." },
];

/** Guided mode shell (M3). The persona toggle works today; the task flows land in v0.4. */
export function GuidedPage() {
  const setPersona = useUi((s) => s.setPersona);
  return (
    <>
      <PageHeader
        title="Guided mode"
        description="A task-by-task path for analysts, built on the same specs Expert mode edits. It is planned for v0.4; until then, everything here is available in Expert mode."
        actions={
          <Button asChild onClick={() => setPersona("expert")}>
            <Link to="/">Switch to Expert mode</Link>
          </Button>
        }
      />
      <ol className="grid max-w-3xl gap-3">
        {TASKS.map((t, i) => (
          <li key={t.title} className="grid grid-cols-[2rem_1fr] gap-3 rounded-lg border bg-card p-4 opacity-80">
            <span className="flex size-7 items-center justify-center rounded-full border text-xs font-semibold text-muted-foreground tabular">{i + 1}</span>
            <div>
              <p className="font-medium">{t.title}</p>
              <p className="text-muted-foreground">{t.body}</p>
            </div>
          </li>
        ))}
      </ol>
    </>
  );
}
