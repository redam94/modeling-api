/**
 * The budget plan in plain words for Guided mode: the goal (from the objective code, not the
 * planning library's `objective_label`) and the planning notes, which mmm-framework writes for
 * modellers ("the greedy allocator's exactness precondition fails"). Notes this module does
 * not know stay with the experts.
 */
import { listWords } from "@/features/guided/plain";

/** "mean" → "the most likely revenue", "p10" → "a very cautious plan: …", "cvar10" → … */
export function objectivePlain(objective: string, kpi: string): string {
  const o = objective.trim().toLowerCase();
  if (o === "mean" || o === "expected") return `the most likely ${kpi}`;
  const cvar = /^cvar(\d+)$/.exec(o);
  if (cvar) return `protect the downside: the average ${kpi} in the worst ${Number(cvar[1])}% of cases`;
  const p = /^p(\d+)$/.exec(o);
  if (p) {
    const q = Number(p[1]);
    const beat = q === 25 ? "3 times in 4" : q === 10 ? "9 times in 10" : q === 50 ? "1 time in 2" : `${100 - q} times in 100`;
    return `${q <= 10 ? "a very cautious plan" : "a cautious plan"}: the ${kpi} you beat ${beat}`;
  }
  return `the ${kpi} by the rule "${objective}"`;
}

type Rule = [RegExp, (m: RegExpExecArray) => string];

const RULES: readonly Rule[] = [
  [
    /^Non-concave response curve\(s\) detected for (.+?):/,
    (m) => {
      const channels = m[1]!.split(/,\s*/).filter(Boolean);
      return channels.length === 1
        ? `The model sees ${channels[0]}'s effect pick up before it tails off, so this is the best plan found, not one proven to be the very best.`
        : `The model sees the effects of ${listWords(channels)} pick up before they tail off, so this is the best plan found, not one proven to be the very best.`;
    },
  ],
  [/^Frontier optimizes/, () => "“What other budgets would bring” shows the best plan for each total budget. Adding money is worth it while each extra unit brings back more than it costs."],
  [
    /^(\d+) channel\(s\) are recommended beyond the spend range/,
    (m) => {
      const n = Number(m[1]);
      return `${n} channel${n === 1 ? "" : "s"} would spend more than the data has seen, so the figures there are less certain. Try a smaller increase first, or test it, before a big one.`;
    },
  ],
  [/^Per-channel caps above ([\d.e+-]+)x current spend are clamped/, (m) => `Limits above ${m[1]} times today's spend were lowered to ${m[1]} times, the most the model can judge.`],
  [/^Total budget exceeds the sum of per-channel caps/, () => "The budget is more than the channel limits allow in total, so part of it could not be placed."],
  [/^The constrained allocator's inner solver failed/, () => "Part of the search for the best plan did not finish cleanly. The plan is usable, but its ranges may be a little too narrow."],
  [/^Near-zero posterior spread/, () => "The ranges look unusually narrow; treat the figures with some caution."],
];

/** Plain sentences for the notes this module knows; the rest (as written) for experts. */
export function planNotes(notes: readonly string[]): { plain: string[]; technical: string[] } {
  const plain: string[] = [];
  const technical: string[] = [];
  for (const note of notes) {
    let said: string | null = null;
    for (const [re, say] of RULES) {
      const m = re.exec(note.trim());
      if (m) {
        said = say(m);
        break;
      }
    }
    if (said === null) technical.push(note);
    else if (!plain.includes(said)) plain.push(said);
  }
  return { plain, technical };
}
