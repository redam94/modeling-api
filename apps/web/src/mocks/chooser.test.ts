/**
 * The mock's port of `mapi_spec.about.recommend` against the rankings the Python function
 * produced for the same About cards (`explorer.gen.json` → `chooser_cases`), and the answer
 * checks of `chooser_answers_from_query`.
 */
import { describe, expect, it } from "vitest";

import type { Recommendation } from "@/features/about/types";

import { answersFromQuery, ChooserAnswerError, recommend } from "./chooser";
import { FIXTURE } from "./explore";

const STARTING = new Set(["model", "template", "workflow_template"]);
const BLOCKS = new Set(["step", "node", "check"]);

describe("recommend (TypeScript port)", () => {
  it.each(FIXTURE.chooser_cases.map((c, i) => [i, c] as const))("replays case %i like the Python ranking", (_i, c) => {
    const params = new URLSearchParams();
    // In the questionnaire's order, as the app sends them (reasons follow the query order).
    for (const q of FIXTURE.chooser.questions) if (c.query[q.id]) params.set(q.id, c.query[q.id]!.join(","));
    const answers = answersFromQuery(params, FIXTURE.chooser.questions);
    const candidates = FIXTURE.abouts.filter((a) => (c.kind ? a.kind === c.kind : STARTING.has(a.kind)));
    const fallback = c.kind ? undefined : FIXTURE.abouts.filter((a) => BLOCKS.has(a.kind));
    const got = recommend(FIXTURE.chooser.questions, answers, candidates, { limit: c.limit, fallback });
    const want = c.result as { items: Recommendation[]; answered: string[]; unanswered: string[] };
    expect(got.answered).toEqual(want.answered);
    expect(got.unanswered).toEqual(want.unanswered);
    expect(got.items?.map((r) => `${r.kind}:${r.id}`)).toEqual(want.items.map((r) => `${r.kind}:${r.id}`));
    got.items?.forEach((r, k) => {
      const w = want.items[k]!;
      expect(r.score).toBeCloseTo(w.score, 4);
      expect(r.reasons_plain).toEqual(w.reasons_plain ?? []);
      expect(r.cautions_plain).toEqual(w.cautions_plain ?? []);
    });
  });

  it("refuses unknown questions and answers, and several answers to a single-answer question", () => {
    const q = FIXTURE.chooser.questions;
    expect(() => answersFromQuery(new URLSearchParams("mood=happy"), q)).toThrow(ChooserAnswerError);
    expect(() => answersFromQuery(new URLSearchParams("goal=fly"), q)).toThrow(/unknown answers/);
    expect(() => answersFromQuery(new URLSearchParams("history=short,long"), q)).toThrow(/takes one answer/);
    expect(answersFromQuery(new URLSearchParams("goal=forecast,check_data&kind=model&limit=3"), q)).toEqual({ goal: ["forecast", "check_data"] });
  });
});
