/**
 * "Add your own" (v0.18 §3.4): the two ways to extend the catalog, side by side. **No code**
 * opens the matching builder in Make your own (a model recipe, a prep preset, a chart template,
 * a team workflow template); **With code** shows the exact `mapi plugin init` command with a
 * copy button and where the docs walk through it.
 *
 * Mounted on the Catalog, the Build palette footer (`variant="compact"`), the workflow gallery
 * and the plot catalog, each with its `kind`. Signed-in surfaces only (never in share or client
 * chunks). Warm copy (relaxed voice, plan D28); token classes only (Studio).
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, Check, Copy, Terminal } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";

import { PANEL_PLUGIN_KIND, PLUGIN_KINDS, pluginInitCommand, type PluginKind } from "@/features/extend/api";
import type { LibrarySearch } from "@/features/library/search";
import { cn } from "@/lib/utils";

export type AddYourOwnKind = "any" | "model" | "step" | "plot" | "workflow_node" | "workflow_template";

interface NoCodeOption {
  key: string;
  title: string;
  body: string;
  /** Make your own (`/library`) with these search params, or another route. */
  to: { library: LibrarySearch } | { route: "/workflows" | "/build/new" };
}

const OPTIONS: Record<Exclude<AddYourOwnKind, "any">, NoCodeOption[]> = {
  model: [
    { key: "model", title: "Describe a model", body: "Pick what you predict, the drivers and how they behave. We turn it into a model you can fit.", to: { library: { tab: "models", new: "model" } } },
    { key: "preset", title: "Save a model preset", body: "Keep your favourite settings for any catalog model, and choose what people may change.", to: { library: { tab: "models", new: "preset" } } },
  ],
  step: [{ key: "prep", title: "Save a prep preset", body: "Bundle the steps you always run, like carry-over then curves, and reuse them on any dataset.", to: { library: { tab: "prep", new: "prep" } } }],
  plot: [{ key: "chart", title: "Make a chart template", body: "Restyle a chart you like, then show it on every run and model page.", to: { library: { tab: "charts", new: "chart" } } }],
  workflow_template: [
    { key: "workflow", title: "Save a workflow template", body: "Open a workflow that works, then choose Save as team template. Your team starts from it in the gallery.", to: { route: "/workflows" } },
  ],
  workflow_node: [
    { key: "workflow", title: "Save a workflow template", body: "Most new workflow steps are a template away: save a working workflow and your team starts from it.", to: { route: "/workflows" } },
  ],
};

/** The no-code options of a panel (`any`: one of each kind). */
export function noCodeOptions(kind: AddYourOwnKind): NoCodeOption[] {
  if (kind !== "any") return OPTIONS[kind];
  return [OPTIONS.model[0]!, OPTIONS.step[0]!, OPTIONS.plot[0]!, OPTIONS.workflow_template[0]!];
}

/** The `--kind` the "With code" column starts on. */
export function defaultPluginKind(kind: AddYourOwnKind): PluginKind {
  return kind === "any" ? "model" : PANEL_PLUGIN_KIND[kind];
}

const PLUGIN_KIND_LABEL: Record<PluginKind, string> = {
  model: "Model",
  step: "Prep step",
  recipe: "Chart",
  node: "Workflow node",
  template: "Workflow template",
};

function OptionLink({ option, className, children }: { option: NoCodeOption; className: string; children: ReactNode }) {
  if ("library" in option.to) {
    return (
      <Link to="/library" search={option.to.library} className={className}>
        {children}
      </Link>
    );
  }
  return (
    <Link to={option.to.route} className={className}>
      {children}
    </Link>
  );
}

/** Copy `text`; `done` is true for a moment after it worked. */
function useCopy(): { copy: (text: string) => void; done: boolean; failed: boolean } {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = setTimeout(() => setState("idle"), 1600);
    return () => clearTimeout(t);
  }, [state]);
  const copy = (text: string) => {
    const clip = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
    if (!clip) {
      setState("failed");
      return;
    }
    clip.writeText(text).then(
      () => setState("done"),
      () => setState("failed"),
    );
  };
  return { copy, done: state === "done", failed: state === "failed" };
}

function CommandBox({ kind, compact }: { kind: AddYourOwnKind; compact?: boolean }) {
  const [pluginKind, setPluginKind] = useState<PluginKind>(() => defaultPluginKind(kind));
  const command = pluginInitCommand(pluginKind);
  const { copy, done, failed } = useCopy();
  const groupId = useId();
  return (
    <div className="grid gap-2.5">
      {compact ? null : (
        <div role="group" aria-labelledby={groupId} className="flex flex-wrap items-center gap-1.5">
          <span id={groupId} className="sr-only">
            What to make
          </span>
          {PLUGIN_KINDS.map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={pluginKind === k}
              onClick={() => setPluginKind(k)}
              className={cn(
                "focus-ring inline-flex min-h-8 items-center rounded-full border-[1.5px] px-3 text-xs font-semibold transition-colors",
                pluginKind === k ? "border-foreground bg-foreground text-background" : "border-border-strong bg-card text-foreground hover:border-primary",
              )}
            >
              {PLUGIN_KIND_LABEL[k]}
            </button>
          ))}
        </div>
      )}
      <div className="flex min-w-0 items-stretch overflow-hidden rounded-xl bg-block text-block-foreground">
        <code data-testid="plugin-command" className="min-w-0 flex-1 px-3 py-2.5 font-mono text-xs leading-5 whitespace-pre-wrap [overflow-wrap:anywhere]">
          <span aria-hidden className="text-block-muted select-none">$ </span>
          {command}
        </code>
        <button
          type="button"
          onClick={() => copy(command)}
          aria-label="Copy the command"
          className="focus-ring inline-flex shrink-0 items-center gap-1.5 bg-block-raised px-3 text-xs font-semibold hover:bg-highlight hover:text-highlight-foreground"
        >
          {done ? <Check aria-hidden className="size-4" /> : <Copy aria-hidden className="size-4" />}
          <span className={cn(compact && "sr-only")}>{done ? "Copied" : "Copy"}</span>
        </button>
      </div>
      <p role="status" aria-live="polite" className={cn("text-xs", failed ? "text-destructive" : "sr-only")}>
        {done ? "The command is copied." : failed ? "The browser did not allow the copy. Select the command and copy it." : ""}
      </p>
    </div>
  );
}

/**
 * The panel. `kind` picks the builder the "No code" side opens and the `--kind` of the command;
 * `any` (the Catalog) shows one option of each kind.
 */
export function AddYourOwn({
  kind = "any",
  variant = "panel",
  headingLevel = 2,
  className,
}: {
  kind?: AddYourOwnKind;
  variant?: "panel" | "compact";
  headingLevel?: 2 | 3;
  className?: string;
}) {
  const Heading = headingLevel === 3 ? "h3" : "h2";
  const titleId = useId();
  const options = noCodeOptions(kind);
  if (variant === "compact") {
    const first = options[0]!;
    return (
      <section aria-labelledby={titleId} data-testid="add-your-own" data-tour="extend-add-own" className={cn("grid gap-3 rounded-2xl border bg-card p-4", className)}>
        <Heading id={titleId} className="text-lg leading-tight font-bold">
          Add your own
        </Heading>
        <div data-tour="extend-no-code" className="grid gap-1">
          <p className="text-xs font-bold text-highlight-ink">No code</p>
          <OptionLink option={first} className="focus-ring inline-flex items-center gap-1 rounded-md text-sm font-semibold text-primary hover:underline">
            {first.title} <ArrowRight aria-hidden className="size-3.5" />
          </OptionLink>
        </div>
        <div data-tour="extend-with-code" className="grid gap-1.5">
          <p className="text-xs font-bold text-attention">With code</p>
          <CommandBox kind={kind} compact />
        </div>
      </section>
    );
  }
  return (
    <section
      aria-labelledby={titleId}
      data-testid="add-your-own"
      data-tour="extend-add-own"
      className={cn("grid overflow-hidden rounded-2xl border bg-card lg:grid-cols-[minmax(0,0.85fr)_minmax(0,2fr)]", className)}
    >
      <div className="flex flex-col gap-3 bg-highlight p-6 text-highlight-foreground sm:p-8">
        <Heading id={titleId} className="display-title text-[clamp(1.75rem,4vw,2.25rem)] leading-[1.02]">
          Add your own
        </Heading>
        <p className="max-w-[40ch] text-sm leading-relaxed">
          Missing something? Make it. Most of it needs no code: build it from a form and it shows up here for your team. Developers can add a plugin too.
        </p>
      </div>
      <div className="grid gap-0 md:grid-cols-2">
        <div data-tour="extend-no-code" className="grid content-start gap-3 p-5 sm:p-6">
          <h3 className="text-lg leading-tight font-bold">No code</h3>
          <ul className="grid gap-2">
            {options.map((o) => (
              <li key={o.key}>
                <OptionLink
                  option={o}
                  className="focus-ring group grid gap-1 rounded-xl border-[1.5px] border-transparent bg-background p-3.5 text-left transition-colors hover:border-primary"
                >
                  <span className="flex items-center justify-between gap-2 font-display text-base font-bold">
                    {o.title}
                    <ArrowRight aria-hidden className="size-4 shrink-0 text-primary transition-transform group-hover:translate-x-0.5" />
                  </span>
                  <span className="text-xs text-muted-foreground">{o.body}</span>
                </OptionLink>
              </li>
            ))}
          </ul>
        </div>
        <div data-tour="extend-with-code" className="grid content-start gap-3 border-t p-5 sm:p-6 md:border-t-0 md:border-l">
          <h3 className="flex items-center gap-2 text-lg leading-tight font-bold">
            <Terminal aria-hidden className="size-4 text-attention" /> With code
          </h3>
          <p className="text-xs text-muted-foreground">Scaffold a Python plugin with one command. It comes with a filled-in About card and a test that checks it.</p>
          <CommandBox kind={kind} />
          <p className="text-xs text-muted-foreground">
            Then <code className="font-mono">mapi plugin test</code> and <code className="font-mono">mapi plugin doctor</code>. The docs walk you through it in Extend › Your first extensions, including how to run it on your own stack.
          </p>
        </div>
      </div>
    </section>
  );
}
