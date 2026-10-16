/**
 * The mock API's "Help me choose": a port of `mapi_spec.about.recommend` (pure and
 * deterministic) so `GET /v1/catalog/recommendations` ranks the real About cards in mock mode.
 * `chooser.test.ts` replays the rankings the Python function produced
 * (`explorer.gen.json` → `chooser_cases`).
 */
import type { About, AboutKind, ChooserQuestion, ChooserResult, Recommendation } from "@/features/about/types";

const DIFFICULTY_RANK = { beginner: 0, intermediate: 1, expert: 2 } as const;
const RUNTIME_RANK = { instant: 0, seconds: 1, minutes: 2, long: 3 } as const;
const SPEED_MAX = { seconds: 1, minutes: 2, can_wait: 3 } as const;

export class ChooserAnswerError extends Error {
  constructor(
    message: string,
    readonly pointer: string,
  ) {
    super(message);
  }
}

/** Answers from a query (`?goal=a,b&data_shape=panel`), checked like `chooser_answers_from_query`. */
export function answersFromQuery(params: URLSearchParams, questions: readonly ChooserQuestion[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  const keys = [...new Set([...params.keys()])];
  for (const key of keys) {
    if (key === "kind" || key === "limit") continue;
    const values = params
      .getAll(key)
      .flatMap((v) => v.split(","))
      .map((v) => v.trim())
      .filter(Boolean);
    const q = questions.find((x) => x.id === key);
    if (!q) throw new ChooserAnswerError(`unknown chooser question '${key}'`, `/${key}`);
    const allowed = new Set(q.choices.map((c) => c.value));
    const bad = values.filter((v) => !allowed.has(v));
    if (!values.length || bad.length) throw new ChooserAnswerError(`${key}: unknown answers ${JSON.stringify(bad)}`, `/${key}`);
    if (!q.multiple && values.length > 1) throw new ChooserAnswerError(`${key} takes one answer`, `/${key}`);
    out[key] = values;
  }
  return out;
}

const round4 = (x: number) => Math.round(x * 1e4) / 1e4;

export function recommend(
  questions: readonly ChooserQuestion[],
  answers: Record<string, string[]>,
  candidates: readonly { kind: AboutKind; id: string; about: About }[],
  opts: { limit?: number; includeAbout?: boolean } = {},
): ChooserResult {
  const byId = new Map(questions.map((q) => [q.id, q]));
  const given = new Map(Object.entries(answers).map(([k, v]) => [k, new Set(v)]));
  const exp = given.get("experience");
  const expRank = exp ? Math.min(...[...exp].map((v) => DIFFICULTY_RANK[v as keyof typeof DIFFICULTY_RANK])) : null;
  const speed = given.get("speed");
  const speedCap = speed ? Math.min(...[...speed].map((v) => SPEED_MAX[v as keyof typeof SPEED_MAX])) : null;
  const scored: { score: number; matches: number; diff: number; id: string; rec: Recommendation; goal: boolean }[] = [];
  for (const { kind, id, about } of candidates) {
    let points = 0;
    let total = 0;
    const reasons: string[] = [];
    const cautions: string[] = [];
    let goalMatch = false;
    for (const [qid, values] of given) {
      const q = byId.get(qid);
      if (!q || qid === "experience" || qid === "speed") continue;
      const fit = about.fits?.[qid];
      if (!fit) continue;
      const weight = q.weight ?? 1;
      total += weight;
      const labels = new Map(q.choices.map((c) => [c.value, c.label]));
      const hit = [...values].filter((v) => fit.includes(v)).sort();
      if (hit.length) {
        points += weight;
        reasons.push(`Suits: ${(labels.get(hit[0]!) ?? hit[0]!).toLowerCase()}`);
        goalMatch = goalMatch || qid === "goal";
      } else {
        points -= weight;
        const first = [...values].sort()[0]!;
        cautions.push(`Not made for: ${(labels.get(first) ?? first).toLowerCase()}`);
      }
    }
    if (expRank !== null) {
      total += 1;
      const gap = DIFFICULTY_RANK[about.difficulty] - expRank;
      if (gap > 0) {
        points -= gap;
        cautions.push(`Needs ${about.difficulty}-level statistics knowledge`);
      } else points += 1;
    }
    if (speedCap !== null) {
      total += 1;
      const gap = RUNTIME_RANK[about.typical_runtime] - speedCap;
      if (gap > 0) {
        points -= gap;
        cautions.push(`Usually takes ${about.typical_runtime}`);
      } else points += 1;
    }
    const score = total === 0 ? 0.5 : Math.max(0, Math.min(1, 0.5 + points / (2 * total)));
    scored.push({
      score: round4(score),
      matches: reasons.length,
      diff: DIFFICULTY_RANK[about.difficulty],
      id,
      goal: goalMatch,
      rec: {
        kind,
        id,
        title: about.title,
        summary_plain: about.summary_plain,
        score: round4(score),
        reasons_plain: reasons,
        cautions_plain: cautions,
        difficulty: about.difficulty,
        typical_runtime: about.typical_runtime,
        ...(opts.includeAbout ? { about } : {}),
      },
    });
  }
  let pool = scored;
  if (given.has("goal") && scored.some((s) => s.goal)) pool = scored.filter((s) => s.goal);
  // ties: more answers fitted explicitly first, then the easier item, then the id
  pool.sort((a, b) => b.score - a.score || b.matches - a.matches || a.diff - b.diff || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return {
    items: pool.slice(0, Math.max(0, opts.limit ?? 10)).map((s) => s.rec),
    answered: questions.filter((q) => given.has(q.id)).map((q) => q.id),
    unanswered: questions.filter((q) => !given.has(q.id)).map((q) => q.id),
  };
}
