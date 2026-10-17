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
        ? `The effect of ${channels[0]} increases and then decreases. Thus, this plan is the best plan that the model found. It is possibly not the very best plan.`
        : `The effects of ${listWords(channels)} increase and then decrease. Thus, this plan is the best plan that the model found. It is possibly not the very best plan.`;
    },
  ],
  [/^Frontier optimizes/, () => "“What other budgets would bring” shows the best plan for each total budget. More money is a good decision while each extra unit gives back more than it costs."],
  [
    /^(\d+) channel\(s\) are recommended beyond the spend range/,
    (m) => {
      const n = Number(m[1]);
      return `${n} channel${n === 1 ? "" : "s"} will spend more than the data shows. Thus, the figures for these channels are less certain. Before a big increase, try a smaller increase or test it.`;
    },
  ],
  [/^Per-channel caps above ([\d.e+-]+)x current spend are clamped/, (m) => `The model lowered the limits above ${m[1]} times today's spend to ${m[1]} times. This is the maximum that the model can estimate.`],
  [/^Total budget exceeds the sum of per-channel caps/, () => "The budget is more than the total of the channel limits. Thus, the model did not put all of the budget in the channels."],
  [/^The constrained allocator's inner solver failed/, () => "Part of the search for the best plan did not finish correctly. You can use the plan, but its ranges can be a little too narrow."],
  [/^Near-zero posterior spread/, () => "The ranges are unusually narrow. Be careful when you use the figures."],
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
