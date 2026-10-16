/**
 * The frame of every Guided step: where you are in the task, what the step answers, "View as
 * YAML" (the real ModelSpec, read-only) with the way to Expert mode, and the "advanced settings
 * applied" note when Expert edits changed what Guided mode keeps fixed.
 */
import { Link, useNavigate } from "@tanstack/react-router";
import { Check, ChevronLeft, FileCode2, SlidersHorizontal } from "lucide-react";
import { lazy, Suspense, type ReactNode } from "react";

import { Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tooltip } from "@/components/ui/overlays";
import { Badge, Progress } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { useEnterGuided, useGuidedLock, useSetPersona } from "./persona";
import type { GuidedStepId, StepDef } from "./session";
import { Sheet, SheetContent, SheetTrigger } from "./ui";

// Monaco only loads when the drawer opens.
const YamlView = lazy(() => import("./yaml-view"));

export interface StepLink {
  def: StepDef;
  /** Where the step lives (`null`: not reachable yet). */
  to: { specId: string; step: GuidedStepId } | null;
  done: boolean;
}

function Stepper({ steps, current }: { steps: StepLink[]; current: GuidedStepId }) {
  const index = Math.max(0, steps.findIndex((s) => s.def.id === current));
  const here = steps[index];
  return (
    <nav aria-label="Steps" className="grid gap-2">
      {/* phones: where you are, as text and a bar */}
      <div className="grid gap-1.5 md:hidden">
        <p className="text-xs font-medium text-muted-foreground">
          Step {index + 1} of {steps.length}
          {here ? <span className="text-foreground"> · {here.def.title}</span> : null}
        </p>
        <Progress value={(index + 1) / steps.length} label="Progress through the steps" className="h-1" />
      </div>
      <ol className="hidden items-center gap-1 md:flex">
        {steps.map((s, i) => {
          const isCurrent = s.def.id === current;
          const body = (
            <>
              <span
                aria-hidden
                className={cn(
                  "flex size-5 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold tabular",
                  isCurrent && "border-primary bg-primary text-primary-foreground",
                  !isCurrent && s.done && "border-success/40 bg-success/12 text-success",
                )}
              >
                {s.done && !isCurrent ? <Check className="size-3" /> : i + 1}
              </span>
              <span className="truncate">{s.def.title}</span>
              {s.done && !isCurrent ? <span className="sr-only">(done)</span> : null}
            </>
          );
          const cls = cn(
            "flex min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-[13px] font-medium",
            isCurrent ? "text-foreground" : "text-muted-foreground",
          );
          return (
            <li key={s.def.id} className="flex min-w-0 items-center gap-1">
              {i > 0 ? <span aria-hidden className="h-px w-4 shrink-0 bg-border lg:w-6" /> : null}
              {s.to && !isCurrent ? (
                <Link to="/guided/$specId/$step" params={s.to} className={cn(cls, "hover:bg-accent hover:text-foreground")}>
                  {body}
                </Link>
              ) : (
                <span className={cls} aria-current={isCurrent ? "step" : undefined} aria-disabled={!s.to && !isCurrent ? true : undefined}>
                  {body}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export function YamlDrawer({ yaml, specId, path }: { yaml: string | null; specId: string | null; path: string }) {
  const setPersona = useSetPersona();
  const lock = useGuidedLock();
  const navigate = useNavigate();
  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button variant="outline" size="sm">
          <FileCode2 /> View as YAML
        </Button>
      </SheetTrigger>
      <SheetContent
        title="Your model, as YAML"
        description="Every answer on these screens fills in this specification. It is the same document Expert mode edits."
      >
        {yaml ? (
          <Suspense fallback={<Spinner />}>
            <YamlView value={yaml} path={path} />
          </Suspense>
        ) : (
          <p className="text-muted-foreground">Answer the questions first: the specification appears as soon as the required ones are answered.</p>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
          <p className="max-w-[46ch] text-xs text-muted-foreground">
            {lock.locked
              ? "This project is locked to Guided mode, so only a project admin can edit the YAML."
              : specId
                ? "Expert mode lets you change any setting. Guided mode keeps your changes and marks them as advanced settings."
                : "Save your answers (upload the data) to open the specification in Expert mode."}
          </p>
          {!lock.locked ? (
            <Button
              size="sm"
              disabled={!specId}
              onClick={() => {
                if (!specId) return;
                setPersona("expert");
                void navigate({ to: "/build/$specId", params: { specId } });
              }}
            >
              <SlidersHorizontal /> Edit in Expert
            </Button>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

export function GuidedFrame({
  templateTitle,
  sessionName,
  steps,
  current,
  yaml,
  specId,
  advanced = [],
  children,
}: {
  templateTitle: string;
  sessionName: string | null;
  steps: StepLink[];
  current: GuidedStepId;
  yaml: string | null;
  specId: string | null;
  advanced?: string[];
  children: ReactNode;
}) {
  const def = steps.find((s) => s.def.id === current)?.def;
  useEnterGuided();
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-6">
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Link to="/guided" className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground">
            <ChevronLeft className="size-3.5" /> All analyses
          </Link>
          <div className="flex flex-wrap items-center gap-2">
            {advanced.length ? (
              <Tooltip content={`Changed in Expert mode: ${advanced.join(", ")}. Guided mode keeps these as they are.`}>
                <Badge variant="info" tabIndex={0} data-testid="advanced-settings">
                  Advanced settings applied
                </Badge>
              </Tooltip>
            ) : null}
            <YamlDrawer yaml={yaml} specId={specId} path={`guided/${specId ?? "draft"}.mapi.yaml`} />
          </div>
        </div>
        <Stepper steps={steps} current={current} />
        <header className="grid gap-1">
          <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {templateTitle}
            {sessionName ? <span className="normal-case"> · {sessionName}</span> : null}
          </p>
          <h1 className="text-[26px] leading-tight font-semibold tracking-[-0.015em] text-balance">{def?.title}</h1>
          {def?.question ? <p className="max-w-[68ch] text-muted-foreground">{def.question}</p> : null}
        </header>
      </div>
      {children}
    </div>
  );
}

/** The back / continue row at the bottom of a step. */
export function StepActions({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("flex flex-wrap items-center justify-between gap-3 border-t pt-5", className)}>{children}</div>;
}
