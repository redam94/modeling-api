/**
 * Ranking of search results (v0.21). `fuzzyScore` is the v0.1 palette score and stays as it
 * was; `matchText` adds the order of the global search on top of it:
 * exact id, then title prefix, word start, substring and subsequence, with a match of all the
 * words of the query before a match of some. Ties go to the newest row, then to the alphabet.
 */

/** Score a label against a query: substring first, then in-order subsequence. */
export function fuzzyScore(label: string, query: string): number {
  const l = label.toLowerCase();
  const q = query.toLowerCase().trim();
  if (!q) return 1;
  const idx = l.indexOf(q);
  if (idx >= 0) return 100 - idx;
  let pos = 0;
  for (const ch of q) {
    pos = l.indexOf(ch, pos);
    if (pos < 0) return 0;
    pos += 1;
  }
  return 10;
}

/** A matched part of a title: `[start, end)` in UTF-16 units. */
export type Range = readonly [start: number, end: number];

export interface TextMatch {
  score: number;
  ranges: Range[];
}

export const SCORE = { id: 1000, all: 100, prefix: 40, word: 30, substring: 20, subsequence: 10 } as const;

interface Part {
  tier: number;
  ranges: Range[];
}

const WORD_CHAR = /[\p{L}\p{N}]/u;

/** `needle` as one piece of `lower`: at the start, at the start of a word, or anywhere. */
function contiguous(lower: string, needle: string): Part | null {
  const first = lower.indexOf(needle);
  if (first < 0) return null;
  if (first === 0) return { tier: SCORE.prefix, ranges: [[0, needle.length]] };
  for (let idx = first; idx >= 0; idx = lower.indexOf(needle, idx + 1)) {
    if (!WORD_CHAR.test(lower[idx - 1] ?? "")) return { tier: SCORE.word, ranges: [[idx, idx + needle.length]] };
  }
  return { tier: SCORE.substring, ranges: [[first, first + needle.length]] };
}

/**
 * The letters of `needle` in order in `lower`, in the shortest piece that holds them. Letters
 * that are far apart (a piece longer than two times the needle) are not a match.
 */
function subsequence(lower: string, needle: string): Part | null {
  if (needle.length < 2) return null;
  const head = needle[0] ?? "";
  let best: number[] | null = null;
  for (let start = lower.indexOf(head); start >= 0; start = lower.indexOf(head, start + 1)) {
    const at = [start];
    let pos = start + 1;
    for (let k = 1; k < needle.length; k++) {
      pos = lower.indexOf(needle[k] ?? "", pos);
      if (pos < 0) break;
      at.push(pos);
      pos += 1;
    }
    // A later start has less text after it: it cannot match either.
    if (at.length < needle.length) break;
    const span = (at.at(-1) ?? start) - start + 1;
    if (span <= needle.length * 2 && (!best || span < (best.at(-1) ?? 0) - (best[0] ?? 0) + 1)) best = at;
  }
  return best ? { tier: SCORE.subsequence, ranges: best.map((i): Range => [i, i + 1]) } : null;
}

function merge(ranges: Range[]): Range[] {
  const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
  const out: [number, number][] = [];
  for (const [start, end] of sorted) {
    const last = out.at(-1);
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else out.push([start, end]);
  }
  return out;
}

/** The query in lower case with single spaces. */
export function normaliseQuery(query: string): string {
  return query.toLowerCase().trim().split(/\s+/).join(" ");
}

/**
 * How well `title` (or the exact `id`) answers `query`; `null` when it does not. `loose`
 * (default) also accepts the letters of a word in order ("dts" for "Datasets").
 */
export function matchText(query: string, title: string, id?: string, loose = true): TextMatch | null {
  const q = normaliseQuery(query);
  if (!q) return null;
  if (id && id.toLowerCase() === q) return { score: SCORE.id, ranges: [] };
  const lower = title.toLowerCase();
  // Lower case changes the length of some letters; the ranges would then point at the wrong text.
  const mark = (ranges: Range[]) => (lower.length === title.length ? merge(ranges) : []);
  const one = (needle: string): Part | null => {
    if (fuzzyScore(lower, needle) === 0) return null;
    return contiguous(lower, needle) ?? (loose ? subsequence(lower, needle) : null);
  };
  const whole = contiguous(lower, q);
  if (whole) return { score: SCORE.all + whole.tier, ranges: mark(whole.ranges) };
  const words = q.split(" ");
  let min = Infinity;
  let max = 0;
  let matched = 0;
  const ranges: Range[] = [];
  for (const word of words) {
    const part = one(word);
    if (!part) continue;
    matched += 1;
    min = Math.min(min, part.tier);
    max = Math.max(max, part.tier);
    ranges.push(...part.ranges);
  }
  if (!matched) return null;
  return { score: matched === words.length ? SCORE.all + min : max, ranges: mark(ranges) };
}

export interface Rankable {
  title: string;
  /** The id of the thing: the query that equals it puts the row first. */
  id?: string | undefined;
  /** When the thing last changed (ISO 8601): the newest wins a tie. */
  at?: string | null | undefined;
  /** Other text that also finds the row (a slug, a catalog id). It is not marked. */
  alt?: string | undefined;
}

export interface Ranked<T> {
  row: T;
  score: number;
  ranges: Range[];
}

export function compareRanked<T extends Rankable>(a: Ranked<T>, b: Ranked<T>): number {
  if (a.score !== b.score) return b.score - a.score;
  const at = (b.row.at ?? "").localeCompare(a.row.at ?? "");
  if (at !== 0) return at;
  return a.row.title.localeCompare(b.row.title, undefined, { sensitivity: "base" });
}

/** The rows that match `query`, best first, at most `limit`. */
export function rankRows<T extends Rankable>(query: string, rows: readonly T[], opts: { limit?: number; loose?: boolean } = {}): Ranked<T>[] {
  const out: Ranked<T>[] = [];
  for (const row of rows) {
    const loose = opts.loose ?? true;
    const m = matchText(query, row.title, row.id, loose);
    if (m) {
      out.push({ row, score: m.score, ranges: m.ranges });
      continue;
    }
    // Found by the other text: one step after the rows that the title found in the same way.
    const alt = row.alt ? matchText(query, row.alt, undefined, loose) : null;
    if (alt) out.push({ row, score: alt.score - 1, ranges: [] });
  }
  out.sort(compareRanked);
  return opts.limit === undefined ? out : out.slice(0, opts.limit);
}
