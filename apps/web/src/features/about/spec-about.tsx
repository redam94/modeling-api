/**
 * The About side panel of the spec editor: the card of the model the spec uses and a plain
 * line for every preparation step, with a way to choose another model.
 */
import { Link } from "@tanstack/react-router";
import { Library, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Mono } from "@/components/common";

import { AboutCard, AboutInline } from "./about-card";
import { useAbout } from "./api";
import { bareId } from "./labels";

export function SpecAboutPanel({ modelUse, steps }: { modelUse: string | null; steps: readonly { id: string; use: string }[] }) {
  const { about, entry, isPending } = useAbout("model", modelUse);
  return (
    <div className="grid gap-5">
      {about && entry ? (
        <AboutCard about={about} kind="model" id={entry.id} />
      ) : (
        <p className="text-[13px] text-muted-foreground">{isPending ? "Loading the catalog…" : modelUse ? `No About card for ${bareId(modelUse)} in this catalog.` : "The spec names no model yet."}</p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button asChild variant="outline" size="sm">
          <Link to="/choose" search={{ kind: "model" }}>
            <Sparkles /> Help me choose a model
          </Link>
        </Button>
        <Button asChild variant="ghost" size="sm">
          <Link to="/catalog" search={{ tab: "models" }}>
            <Library /> All models
          </Link>
        </Button>
      </div>
      {steps.length ? (
        <section className="grid gap-2" aria-labelledby="spec-steps-about">
          <h3 id="spec-steps-about" className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            Preparation steps
          </h3>
          <ul className="grid gap-2">
            {steps.map((s) => (
              <li key={s.id} className="grid gap-0.5 rounded-lg border p-2.5">
                <Mono className="text-xs font-semibold">
                  {s.id} <span className="font-normal text-muted-foreground">{s.use}</span>
                </Mono>
                <AboutInline kind="step" id={s.use} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
