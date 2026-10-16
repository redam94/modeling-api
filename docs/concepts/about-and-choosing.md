---
description: Every model, step, template, workflow node and governance check carries a plain-language About card, and "Help me choose" turns a few questions into ranked recommendations.
---

# About cards and choosing <span class="mapi-badge">v0.5</span>

!!! tip "Just want to pick a model?"
    [Choosing a model](../guides/choosing-a-model.md) shows Help me choose in the web app,
    the terminal (`mapi choose`) and Python (`api.catalog.recommend`), and compares the model
    families.

People who do not do statistics for a living still have to pick a model, a preprocessing
step or a workflow. Every item they can pick from a catalog therefore carries an **About
card** in plain language, and the **Help me choose** chooser turns a few questions about
their goal and their data into a ranked list with the reasons.

## The About card

```json
{
  "title": "Weekly totals",
  "summary_plain": "Adds up daily numbers into one number per week (or month), so that every row covers the same length of time.",
  "use_when": ["Your data is daily but you want to plan or report by week"],
  "avoid_when": ["Your data already has one row per week"],
  "data_needs": ["A date column"],
  "assumptions": ["Numbers can be added up (spend, sales); averages are averaged"],
  "outputs": ["The same columns, one row per week"],
  "difficulty": "beginner",
  "typical_runtime": "instant",
  "example": "Daily TV spend of 100 each day becomes 700 for the week.",
  "related": ["core.calendar"],
  "fits": {"goal": ["check_data"]}
}
```

| Field | What it holds |
|---|---|
| `title` | A human name |
| `summary_plain` | One or two sentences, no jargon: what it does for you |
| `use_when` / `avoid_when` | When it fits and when to pick something else |
| `data_needs` | The data it needs |
| `assumptions` | What it takes for granted |
| `outputs` | What you get out |
| `difficulty` | `beginner`, `intermediate` or `expert` |
| `typical_runtime` | `instant`, `seconds`, `minutes` or `long` |
| `example` | A tiny example of it at work |
| `related` | Ids of related catalog items |
| `expert_notes`, `references` | The maths and the sources, kept apart |
| `fits` | Chooser tags: the answers this item suits |

Cards are attached to model manifests and catalog entries (`ModelManifest.about`,
`StepCatalogEntry.about`), guided templates (`GuidedTemplate.about`), workflow templates,
node kinds and node implementations, and governance checks. Your own specs, pipelines and
workflows can carry one too, in `metadata.about`, so colleagues know when to reuse them.
`GET /v1/catalog/abouts` lists the cards of everything a user can pick (filter with
`kind`): the installed models and preprocessing steps, the splits, guided templates,
workflow templates, workflow node kinds and nodes, and the governance checks. Datasets have a `DatasetAbout`: the owner's own description
plus a summary generated from the schema and coordinate summary, for example *"Weekly data
from 2023-01-01 to 2024-12-29 (104 weeks) for 10 geos and 3 channels, with 3 measures:
revenue (the result you want to explain), spend (marketing spend), price."*

### Complete cards are enforced

`mapi_spec.about.about_issues` is the editorial rule: every required field filled, the
summary one or two sentences without jargon words (such as "posterior", "stationarity",
"autoregressive"), chooser tags that name real questions and answers, and `related` ids that
exist. The catalog rule (`Catalog.about_issues`, problem code `ABOUT_INCOMPLETE` with a JSON
pointer) and the contract kit (`mapi_sdk.testing.check_about`) apply it, so a plugin without a
complete card fails its checks. The built-in cards live in `mapi_spec.abouts`.

## Help me choose

`GET /v1/catalog/chooser` returns the questions (`ChooserQuestionnaire`):

| Question | Answers |
|---|---|
| `goal` - What do you want to do? | measure what marketing adds, plan a budget, forecast, understand drivers, predict per item, understand choices, find regimes, compare series, check data, validate a model |
| `data_shape` - What does your data look like? | one series, a few related series, a panel (markets, stores), choices, a table |
| `frequency` | daily, weekly, monthly, not over time |
| `history` | less than a year, one to three years, more |
| `drivers` - Other columns that could explain the result? | yes, no |
| `uncertainty` - Do you need a range? | essential, nice to have, no |
| `experience` | new to statistics, the basics, expert |
| `speed` | within seconds, within minutes, can wait |

`GET /v1/catalog/recommendations?goal=forecast&data_shape=one_series&experience=beginner&kind=model`
returns a `ChooserResult`: the candidates ranked best first, each with a score between 0 and
1, the reasons it fits ("Suits: forecast the future") and cautions ("Needs expert-level
statistics knowledge", "Usually takes long"). Several answers to one question are separated
by commas. The ranking (`mapi_spec.about.recommend`) is deterministic: a matching answer adds
the question's weight (the goal counts most), a mismatch subtracts it, items that do not mind
a question are neutral, and items harder than your experience or slower than your patience
lose points. When some items match your goal, the others are left out.
Without `kind` the candidates are the places a user starts from: models, guided templates
and workflow templates; `kind=step`, `kind=node` or `kind=check` ranks one kind of
building block. A question or answer the chooser does not know is `422
CHOOSER_ANSWER_INVALID`.
