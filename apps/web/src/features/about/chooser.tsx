/**
 * "Help me choose" (M3.5): a few plain questions about the goal and the data
 * (`GET /v1/catalog/chooser`), answered one at a time, and the catalog items that fit best
 * (`GET /v1/catalog/recommendations`), ranked with the reasons and cautions, each one click
 * from a Guided analysis, a spec or a workflow.
 */
import { Link } from "@tanstack/react-router";
import { AlertTriangle, ArrowLeft, ArrowRight, Check, Info, RotateCcw, Sparkles } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { ErrorNotice, LoadingRows, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Progress } from "@/components/ui/primitives";
import { useEffectivePersona } from "@/features/guided/persona";
import { cn } from "@/lib/utils";

import { AboutDialog, DifficultyBadge, RuntimeBadge } from "./about-card";
import { normaliseAnswers, useChooser, useRecommendations } from "./api";
import { KIND_META } from "./labels";
import { startTarget, type StartTarget } from "./start";
import type { ChooserAnswers, ChooserKind, ChooserQuestion, Recommendation } from "./types";

// ------------------------------------------------------------------------------ start button

export function useStartTarget(kind: Recommendation["kind"], id: string): StartTarget {
  return startTarget(kind, id, useEffectivePersona());
}

/** The primary action of a recommendation or a browsed card. */
export function StartButton({ kind, id, size = "sm", variant = "default", children }: { kind: Recommendation["kind"]; id: string; size?: "sm" | "default"; variant?: "default" | "outline"; children?: ReactNode }) {
  const target = useStartTarget(kind, id);
  const label = (
    <>
      {children ?? target.label} <ArrowRight />
    </>
  );
  return (
    <Button asChild size={size} variant={variant}>
      {target.kind === "guided" ? (
        <Link to={target.to} params={target.params}>
          {label}
        </Link>
      ) : (
        <Link to={target.to} search={target.search}>
          {label}
        </Link>
      )}
    </Button>
  );
}

// ------------------------------------------------------------------------------ one question

function QuestionField({ question, value, onChange }: { question: ChooserQuestion; value: string[]; onChange: (values: string[]) => void }) {
  const legendId = useId();
  const name = useId();
  const multiple = Boolean(question.multiple);
  return (
    <fieldset className="grid gap-3" aria-describedby={question.help_plain ? `${legendId}-help` : undefined}>
      <legend id={legendId} className="text-[19px] leading-snug font-semibold tracking-tight text-balance">
        {question.prompt_plain}
      </legend>
      {question.help_plain || multiple ? (
        <p id={`${legendId}-help`} className="-mt-1 text-[13px] text-muted-foreground">
          {[question.help_plain, multiple ? "Pick all that apply." : null].filter(Boolean).join(" ")}
        </p>
      ) : null}
      <div className="grid gap-2 sm:grid-cols-2">
        {question.choices.map((choice) => {
          const checked = value.includes(choice.value);
          return (
            <label
              key={choice.value}
              className={cn(
                "flex cursor-pointer items-start gap-3 rounded-lg border bg-card px-3.5 py-3 text-[14px] transition-colors hover:border-primary/40 hover:bg-accent/40",
                "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ring",
                checked && "border-primary/70 bg-accent/60",
              )}
            >
              <input
                type={multiple ? "checkbox" : "radio"}
                name={name}
                value={choice.value}
                checked={checked}
                onChange={(e) => {
                  if (multiple) onChange(e.target.checked ? [...value, choice.value] : value.filter((v) => v !== choice.value));
                  else onChange([choice.value]);
                }}
                className="mt-0.5 size-4 shrink-0 accent-primary"
              />
              <span className="grid gap-0.5">
                <span className="font-medium">{choice.label}</span>
                {choice.help_plain ? <span className="text-xs text-muted-foreground">{choice.help_plain}</span> : null}
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

// ------------------------------------------------------------------------------ results

function MatchMeter({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const tone = pct >= 75 ? "bg-success" : pct >= 50 ? "bg-primary" : "bg-warning";
  return (
    <span className="flex items-center gap-2 text-xs text-muted-foreground" title="How well it fits your answers">
      <span className="relative h-1.5 w-16 overflow-hidden rounded-full bg-muted" aria-hidden>
        <span className={cn("absolute inset-y-0 left-0 rounded-full", tone)} style={{ width: `${pct}%` }} />
      </span>
      <span className="font-medium text-foreground tabular">{pct}% match</span>
    </span>
  );
}

export function RecommendationCard({ rec, rank, compact = false }: { rec: Recommendation; rank: number; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="grid gap-2.5 rounded-xl border bg-card p-4" data-recommendation={rec.id} aria-label={`${rank}. ${rec.title}`}>
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <div className="min-w-0">
          <p className="text-[11px] font-medium text-muted-foreground">
            {rank === 1 ? "Best match · " : `#${rank} · `}
            {KIND_META[rec.kind].label}
          </p>
          <p className="font-semibold tracking-tight">{rec.title}</p>
        </div>
        <MatchMeter score={rec.score} />
      </div>
      <p className={cn("text-[13px] text-muted-foreground", compact && "line-clamp-2")}>{rec.summary_plain}</p>
      {rec.reasons_plain?.length || rec.cautions_plain?.length ? (
        <ul className="grid gap-1 text-[13px]">
          {(rec.reasons_plain ?? []).map((r) => (
            <li key={r} className="flex items-start gap-1.5">
              <Check aria-hidden className="mt-0.5 size-3.5 shrink-0 text-success" />
              <span>{r}</span>
            </li>
          ))}
          {(rec.cautions_plain ?? []).map((c) => (
            <li key={c} className="flex items-start gap-1.5">
              <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" />
              <span>{c}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
        <span className="flex flex-wrap gap-1.5">
          <DifficultyBadge difficulty={rec.difficulty} />
          <RuntimeBadge runtime={rec.typical_runtime} />
        </span>
        <span className="flex flex-wrap gap-1.5">
          {rec.about ? (
            <Button variant="ghost" size="sm" onClick={() => setOpen(true)} aria-label={`About ${rec.title}`}>
              <Info /> About
            </Button>
          ) : null}
          <StartButton kind={rec.kind} id={rec.id}>
            Start with this
          </StartButton>
        </span>
      </div>
      {rec.about ? <AboutDialog about={rec.about} kind={rec.kind} id={rec.id} open={open} onOpenChange={setOpen} actions={<StartButton kind={rec.kind} id={rec.id} />} /> : null}
    </li>
  );
}

function Results({ answers, kind, limit, compact }: { answers: ChooserAnswers; kind: ChooserKind | null; limit: number; compact: boolean }) {
  const answered = Object.keys(normaliseAnswers(answers)).length;
  const recs = useRecommendations(answers, { kind, limit, enabled: answered > 0 });
  if (!answered) {
    return (
      <div className="grid place-items-start gap-2 rounded-xl border border-dashed p-5 text-[13px] text-muted-foreground">
        <Sparkles aria-hidden className="size-5 text-primary" />
        <p>Answer the first question and your best matches appear here. They update with every answer.</p>
      </div>
    );
  }
  if (recs.isError) return <ErrorNotice error={recs.error} title="The recommendations could not be loaded" onRetry={() => void recs.refetch()} />;
  if (!recs.data) return <LoadingRows rows={3} />;
  return (
    <div className="grid gap-3" aria-busy={recs.isFetching}>
      <p className="flex items-center gap-2 text-xs text-muted-foreground" aria-live="polite">
        {recs.isFetching ? <Spinner className="size-3" /> : null}
        {(recs.data.items ?? []).length
          ? `Your ${(recs.data.items ?? []).length === 1 ? "best match" : `top ${(recs.data.items ?? []).length} matches`}, from ${answered} answer${answered === 1 ? "" : "s"}.`
          : "Nothing fits these answers. Try changing one."}
      </p>
      <ol className="grid gap-3" aria-label="Recommendations">
        {(recs.data.items ?? []).map((rec, i) => (
          <RecommendationCard key={`${rec.kind}:${rec.id}`} rec={rec} rank={i + 1} compact={compact} />
        ))}
      </ol>
    </div>
  );
}

// ------------------------------------------------------------------------------ the chooser

export interface HelpMeChooseProps {
  answers: ChooserAnswers;
  onAnswersChange: (answers: ChooserAnswers) => void;
  /** Rank one kind of item (default: the starting points - models and templates). */
  kind?: ChooserKind | null;
  /** How many matches to show. */
  limit?: number;
  /** A side-by-side layout for full pages; a stacked one for panels. */
  layout?: "page" | "panel";
  /** Questions to leave out (a panel can ask only the essentials). */
  only?: readonly string[];
}

/** The questionnaire with live results (controlled: the answers live with the caller). */
export function HelpMeChoose({ answers, onAnswersChange, kind = null, limit = 5, layout = "page", only }: HelpMeChooseProps) {
  const chooser = useChooser();
  const [step, setStep] = useState(0);
  if (chooser.isPending) return <LoadingRows rows={4} />;
  if (chooser.isError) return <ErrorNotice error={chooser.error} title="The questions could not be loaded" onRetry={() => void chooser.refetch()} />;
  const questions = chooser.data.questions.filter((q) => !only || only.includes(q.id));
  const at = Math.min(step, questions.length);
  const question = questions[at];
  const done = at >= questions.length;
  const answeredCount = questions.filter((q) => (answers[q.id] ?? []).length > 0).length;
  const set = (id: string, values: string[]) => onAnswersChange({ ...answers, [id]: values });
  const reset = () => {
    onAnswersChange({});
    setStep(0);
  };

  const questionnaire = (
    <div className="grid content-start gap-5 rounded-xl border bg-card p-5 shadow-[0_1px_0_0_rgb(0_0_0/0.03)]">
      <div className="grid gap-2">
        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
          <span aria-live="polite">{done ? "All questions answered or skipped" : `Question ${at + 1} of ${questions.length}`}</span>
          {answeredCount ? (
            <Button variant="ghost" size="sm" onClick={reset}>
              <RotateCcw /> Start over
            </Button>
          ) : null}
        </div>
        <Progress value={questions.length ? Math.min(at, questions.length) / questions.length : 0} label="Questions answered" />
      </div>
      {question ? (
        <QuestionField key={question.id} question={question} value={answers[question.id] ?? []} onChange={(v) => set(question.id, v)} />
      ) : (
        <div className="grid gap-2">
          <p className="text-[17px] font-semibold tracking-tight">That&apos;s everything.</p>
          <p className="text-[13px] text-muted-foreground">Change any answer below; the matches update as you go.</p>
          <ul className="grid gap-1 text-[13px]">
            {questions.map((q, i) => (
              <li key={q.id}>
                <button type="button" className="flex w-full justify-between gap-3 rounded-md px-2 py-1 text-left hover:bg-accent" onClick={() => setStep(i)}>
                  <span className="text-muted-foreground">{q.prompt_plain}</span>
                  <span className="font-medium">
                    {(answers[q.id] ?? []).map((v) => q.choices.find((c) => c.value === v)?.label ?? v).join(", ") || "Skipped"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button variant="ghost" onClick={() => setStep(Math.max(0, at - 1))} disabled={at === 0}>
          <ArrowLeft /> Back
        </Button>
        {!done ? (
          <span className="flex gap-2">
            {(answers[question?.id ?? ""] ?? []).length === 0 ? (
              <Button variant="outline" onClick={() => setStep(at + 1)}>
                Skip
              </Button>
            ) : null}
            <Button onClick={() => setStep(at + 1)} disabled={(answers[question?.id ?? ""] ?? []).length === 0}>
              {at + 1 === questions.length ? "See my matches" : "Next"} <ArrowRight />
            </Button>
          </span>
        ) : null}
      </div>
    </div>
  );

  const results = (
    <section aria-label="Your matches" className="grid content-start gap-3">
      <h2 className="flex items-center gap-2 text-[15px] font-semibold tracking-tight">
        <Sparkles aria-hidden className="size-4 text-primary" /> Your matches
        {kind ? <Badge variant="secondary">{KIND_META[kind].plural}</Badge> : null}
      </h2>
      <Results answers={answers} kind={kind} limit={limit} compact={layout === "panel"} />
    </section>
  );

  return layout === "page" ? (
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      {questionnaire}
      {results}
    </div>
  ) : (
    <div className="grid gap-5 md:grid-cols-2">
      {questionnaire}
      {results}
    </div>
  );
}
