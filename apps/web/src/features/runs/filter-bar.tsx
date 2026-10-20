/**
 * The filter bar of the runs table (v0.14): the run-search filters as chips, and an input that
 * checks a new filter with the TS port of the grammar (`run-query.ts`) before it goes to the
 * URL. The server stays the authority: the page shows its 422 message under the bar.
 */
import { Filter, Plus, X } from "lucide-react";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/primitives";

import { grammarError, MAX_RUN_FILTERS, parseRunFilter } from "./run-query";

export const FILTER_EXAMPLES = ["metric.rmse < 3", "param.lr = 0.01", "tag.team = growth"] as const;

/** The problem of a new filter, in STE (`null` when the bar can add it). */
export function newFilterError(text: string, filters: readonly string[]): string | null {
  const t = text.trim();
  if (!t) return "Type a filter.";
  if (filters.length >= MAX_RUN_FILTERS) return `You can use ${MAX_RUN_FILTERS} filters maximum. Remove a filter first.`;
  if (filters.includes(t)) return "The table already uses this filter.";
  const error = grammarError(parseRunFilter, t);
  return error ? `The filter is not correct: ${error}` : null;
}

export function FilterBar({ filters, onChange, serverError }: { filters: readonly string[]; onChange: (filters: string[]) => void; serverError?: string | null }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  const add = () => {
    const problem = newFilterError(text, filters);
    setError(problem);
    if (problem) return;
    onChange([...filters, text.trim()]);
    setText("");
  };
  return (
    <div className="grid gap-1.5" role="group" aria-label="Filters">
      <div className="flex flex-wrap items-center gap-2">
        <Filter className="size-4 text-muted-foreground" aria-hidden />
        {filters.map((f) => (
          <span key={f} className="inline-flex max-w-full items-center gap-1 rounded-md border bg-accent/40 py-0.5 pr-1 pl-2 font-mono text-xs">
            <span className="[overflow-wrap:anywhere]">{f}</span>
            <button type="button" className="rounded-sm p-0.5 text-muted-foreground hover:text-foreground" aria-label={`Remove the filter ${f}`} onClick={() => onChange(filters.filter((x) => x !== f))}>
              <X className="size-3" />
            </button>
          </span>
        ))}
        <form
          className="flex min-w-0 flex-1 items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            add();
          }}
        >
          <Input
            id={id}
            aria-label="New filter"
            aria-invalid={error ? true : undefined}
            aria-describedby={`${id}-help`}
            placeholder={FILTER_EXAMPLES[0]}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              if (error) setError(null);
            }}
            className="h-8 min-w-48 flex-1 font-mono text-xs sm:max-w-96"
          />
          <Button type="submit" variant="outline" size="sm" disabled={!text.trim()}>
            <Plus /> Add filter
          </Button>
        </form>
      </div>
      {error ? (
        <p role="alert" className="text-xs text-destructive [overflow-wrap:anywhere]">
          {error}
        </p>
      ) : null}
      {serverError ? (
        <p role="alert" className="text-xs text-destructive [overflow-wrap:anywhere]">
          The server did not accept the search: {serverError}
        </p>
      ) : null}
      <p id={`${id}-help`} className="text-xs text-muted-foreground">
        Examples: {FILTER_EXAMPLES.join(", ")}. Use metric, param, tag or name, an operator and a value.
      </p>
    </div>
  );
}
