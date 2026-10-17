/** The budget form's request and its static checks (mirroring `check_feasible`). */
import { describe, expect, it } from "vitest";

import { distinctAmounts } from "@/features/guided/plain";
import { planFor } from "@/mocks/explore";

import { objectivePlain, planNotes } from "./notes";
import { planHeadline } from "./result";
import { defaultForm, planProblems, targetBudget, toRequest, type PlanForm } from "./plan";

const current = { tv: 74_000, search: 30_000 };

function form(patch: Partial<PlanForm> = {}): PlanForm {
  return { ...defaultForm(Object.keys(current)), ...patch };
}

describe("toRequest", () => {
  it("sends only what differs from the defaults", () => {
    expect(toRequest(form({ frontier: false }))).toEqual({ objective: "mean", mode: "fixed" });
    expect(toRequest(form())).toEqual({ objective: "mean", mode: "fixed", frontier: { n_points: 12 } });
  });

  it("maps budgets, bounds in multiples or money, locks, groups and advanced settings", () => {
    const f = form({
      mode: "change",
      changePct: -10,
      objective: "p10",
      byGeo: true,
      horizon: 13,
      frontier: false,
      limits: {
        tv: { unit: "multiplier", min: 0.5, max: 1.5, locked: false },
        search: { unit: "money", min: 20_000, max: null, locked: false },
      },
      groups: [{ name: "digital", channels: ["search"], min_share: 0.2, max_share: null }],
      defaultMin: 0.2,
      seed: 7,
    });
    expect(toRequest(f)).toEqual({
      objective: "p10",
      mode: "fixed",
      budget_change_pct: -10,
      channels: { tv: { min_multiplier: 0.5, max_multiplier: 1.5 }, search: { min_spend: 20_000 } },
      default_bounds: { min_multiplier: 0.2, max_multiplier: 2 },
      groups: [{ name: "digital", channels: ["search"], min_share: 0.2, max_share: null }],
      horizon: 13,
      by_geo: true,
      seed: 7,
    });
    expect(toRequest(form({ mode: "free", valuePerKpi: 1, limits: { tv: { unit: "multiplier", min: null, max: null, locked: true } } }))).toMatchObject({
      mode: "free",
      value_per_kpi: 1,
      channels: { tv: { locked: true } },
    });
  });
});

describe("planProblems", () => {
  it("accepts the defaults", () => {
    expect(planProblems(form(), current)).toEqual([]);
    expect(targetBudget(form(), 104_000)).toBe(104_000);
    expect(targetBudget(form({ mode: "change", changePct: 10 }), 100)).toBeCloseTo(110);
    expect(targetBudget(form({ mode: "free" }), 100)).toBeNull();
  });

  it("explains field problems", () => {
    expect(planProblems(form({ mode: "total", total: null }), current)).toEqual(["Enter a total budget above zero."]);
    expect(planProblems(form({ objective: "p70" }), current)[0]).toMatch(/goal/);
    expect(planProblems(form({ limits: { tv: { unit: "multiplier", min: 2, max: 1, locked: false } } }), current)).toEqual(["tv: the minimum is above the maximum."]);
    expect(planProblems(form({ limits: { tv: { unit: "multiplier", min: 3, max: null, locked: false } } }), current)[0]).toMatch(/usual maximum/);
  });

  it("finds infeasible budgets like the server", () => {
    // minimums above the budget
    const high = planProblems(form({ limits: { tv: { unit: "money", min: 200_000, max: null, locked: false } } }), current);
    expect(high).toEqual(["The channel minimums add up to 200,000, more than the budget of 104,000. Lower some minimums or raise the budget."]);
    // maximums below the budget (a listed channel's unset minimum is 0x, its maximum 2x)
    const low = planProblems(form({ mode: "total", total: 500_000 }), current);
    expect(low[0]).toMatch(/maximums add up to 208,000, less than the budget of 500,000/);
    // a locked channel counts at today's spend on both sides
    expect(planProblems(form({ mode: "total", total: 74_000, limits: { tv: { unit: "multiplier", min: null, max: null, locked: true } } }), current)).toEqual([]);
    expect(planProblems(form({ groups: [{ name: "a", channels: ["tv"], min_share: 0.7 }, { name: "b", channels: ["search"], min_share: 0.5 }] }), current)).toEqual([
      "The groups' minimum shares add up to more than 100%.",
    ]);
  });
});

describe("the plan", () => {
  it("says what moves where, what it adds and how sure that is", () => {
    const plan = planFor({ objective: "mean" });
    const h = planHeadline(plan.result!);
    expect(h.text).toMatch(/^Move money from tv \(−7,491\) to search \(\+7,491\)\. That adds about \+3,063 to revenue \(90% likely between/);
    expect(h.text).toContain("The chance it beats today's plan is 100%.");
    expect(h.tone).toBe("good");
    const bigger = planFor({ total_budget: 150_000 });
    expect(planHeadline(bigger.result!).text).toMatch(/^With 150K instead of 104\.3K: spend more on search \(\+23\.9K\), tv \(\+21\.8K\)\./);
    expect(planFor({ channels: { tv: { min_spend: 500_000 } } }).problem?.code).toBe("OPTIMIZE_INFEASIBLE");
  });
});

describe("the plan in plain words", () => {
  it("names the goal without statistics jargon", () => {
    expect(objectivePlain("mean", "revenue")).toBe("the most likely revenue");
    expect(objectivePlain("p25", "revenue")).toBe("a cautious plan: the revenue you beat 3 times in 4");
    expect(objectivePlain("p10", "revenue")).toBe("a very cautious plan: the revenue you beat 9 times in 10");
    expect(objectivePlain("p5", "sales")).toBe("a very cautious plan: the sales you beat 95 times in 100");
    expect(objectivePlain("cvar10", "revenue")).toBe("protect the downside: the average revenue in the worst 10% of cases");
    for (const o of ["mean", "p25", "p10", "p5", "cvar10"]) expect(objectivePlain(o, "revenue")).not.toMatch(/posterior|percentile|CVaR|quantile/i);
  });

  it("turns the planning notes into plain sentences, or leaves them to experts", () => {
    const notes = planNotes(planFor({}).result!.notes_plain ?? []);
    expect(notes.plain).toEqual([
      "The model sees tv's effect pick up before it tails off, so this is the best plan found, not one proven to be the very best.",
      "“What other budgets would bring” shows the best plan for each total budget. Adding money is worth it while each extra unit brings back more than it costs.",
    ]);
    for (const n of notes.plain) expect(n).not.toMatch(/greedy|allocator|posterior|marginal roi|concave|solver/i);
    expect(notes.technical).toHaveLength(0);
    expect(planNotes(["Some new solver detail."])).toEqual({ plain: [], technical: ["Some new solver detail."] });
    expect(planNotes(["2 channel(s) are recommended beyond the spend range the model has observed; those figures extrapolate."]).plain[0]).toMatch(/^2 channels would spend more than the data has seen/);
  });

  it("shows ranges with enough digits that their ends differ", () => {
    const r = planFor({}).result!;
    const fmt = distinctAmounts([
      [r.expected_kpi.quantiles!["0.05"]!, r.expected_kpi.quantiles!["0.95"]!],
      [r.current_kpi!.quantiles!["0.05"]!, r.current_kpi!.quantiles!["0.95"]!],
      [r.expected_kpi.mean, r.current_kpi!.mean],
    ]);
    expect(fmt(1_304_872)).toBe("1.305M");
    expect(fmt(1_312_200)).toBe("1.312M");
    expect(fmt(1_308_581)).not.toBe(fmt(1_305_518));
    expect(fmt(2_500, "EUR")).toBe("EUR 2,500");
    // nothing to separate: the usual one decimal
    expect(distinctAmounts([[1_000_000, 1_000_000]])(1_234_567)).toBe("1.2M");
    expect(distinctAmounts([[1_200_000, 1_900_000]])(1_234_567)).toBe("1.2M");
  });
});
