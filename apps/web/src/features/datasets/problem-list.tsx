/**
 * The problems of a part of the upload wizard, as a live region (a screen reader announces a
 * new problem as the user types) whose items have ids, so each input can point at the
 * problems that are about it (`aria-describedby`).
 */
import { cn } from "@/lib/utils";

/** The id of the `i`-th problem of the list `id`. */
export function problemId(id: string, i: number): string {
  return `${id}-${i}`;
}

/** `aria-describedby` for the problems `indexes` of the list `id` (plus other ids, such as a hint). */
export function describedBy(id: string, indexes: number[], ...extra: (string | undefined)[]): string | undefined {
  const ids = [...extra.filter((x): x is string => Boolean(x)), ...indexes.map((i) => problemId(id, i))];
  return ids.length ? ids.join(" ") : undefined;
}

/** The indexes of the items of `list` that match `pick`. */
export function indexesOf<T>(list: T[], pick: (item: T) => boolean): number[] {
  const out: number[] = [];
  list.forEach((item, i) => {
    if (pick(item)) out.push(i);
  });
  return out;
}

/**
 * The live region is always in the page (a region that appears with its first message is not
 * always announced); without problems it is `sr-only`, so it takes no row of a grid.
 */
export function ProblemList({ id, label, messages, className }: { id: string; label?: string; messages: string[]; className?: string }) {
  return (
    <div aria-live="polite" className={cn(!messages.length && "sr-only")}>
      {messages.length ? (
        <ul aria-label={label} className={cn("grid gap-0.5 text-xs text-destructive", className)}>
          {messages.map((m, i) => (
            <li key={i} id={problemId(id, i)}>
              {m}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
