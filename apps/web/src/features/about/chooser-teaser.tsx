/**
 * The way into "Help me choose" on Home and in Guided mode: the first question (the goal)
 * as one-click answers that open the chooser with it answered.
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, Sparkles } from "lucide-react";

import { cn } from "@/lib/utils";

import { useChooser } from "./api";

export function ChooserTeaser({ className, title = "Not sure where to start?" }: { className?: string; title?: string }) {
  const chooser = useChooser();
  const goal = chooser.data?.questions.find((q) => q.id === "goal");
  return (
    <section aria-labelledby="chooser-teaser-h" className={cn("grid gap-3 rounded-xl border bg-gradient-to-br from-accent/70 to-card p-5", className)} data-testid="chooser-teaser">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="grid gap-1">
          <h2 id="chooser-teaser-h" className="flex items-center gap-2 text-[17px] font-semibold tracking-tight">
            <Sparkles aria-hidden className="size-4 text-primary" /> {title}
          </h2>
          <p className="text-[13px] text-muted-foreground">Tell us what you want to do. Answer some plain questions. Then you get the applicable analyses and models, with the reasons.</p>
        </div>
        <Link to="/choose" className="inline-flex items-center gap-1 text-[13px] font-medium text-primary hover:underline">
          Help me choose <ArrowRight aria-hidden className="size-3.5" />
        </Link>
      </div>
      {goal ? (
        <ul className="flex flex-wrap gap-2" aria-label={goal.prompt_plain}>
          {goal.choices.map((c) => (
            <li key={c.value}>
              <Link
                to="/choose"
                search={{ goal: c.value }}
                className="inline-flex items-center rounded-full border bg-card px-3 py-1 text-[13px] hover:border-primary/50 hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring"
              >
                {c.label}
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
