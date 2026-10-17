"""Plain-language "About" cards and the "Help me choose" chooser (M3.5).

Every item a user picks from a catalog - models, preprocessing steps and splits, guided
templates and specs, workflow templates, workflow node kinds and node implementations,
governance checks, datasets - carries an :class:`About`: what it is in one or two sentences
without jargon, when to use it and when not to, the data it needs, what it assumes, what you
get out, how hard it is, how long it usually takes, a tiny example and related items. Expert
detail (maths, references) lives apart in ``expert_notes`` and ``references``.

* :func:`about_issues` is the completeness rule the catalog and the contract kit apply
  (``mapi_spec.catalog.catalog_about_issues``, ``mapi_sdk.testing.check_about``);
* :class:`DatasetAbout` combines a user's description of a dataset with a summary generated
  from its schema and coordinate summary (:func:`describe_dataset`, pure);
* the chooser asks :data:`CHOOSER_QUESTIONS` and ranks candidates by their
  :attr:`About.fits` tags (:func:`recommend`, pure), behind
  ``GET /v1/catalog/chooser`` and ``GET /v1/catalog/recommendations``.

Pure Pydantic and stdlib (no numpy): the control plane, the web app and the docs share it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from mapi_spec._dto import RequestModel, ResponseModel, dto_examples
from mapi_spec._types import StrictModel
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema, Role

__all__ = [
    "ABOUT_JARGON",
    "CHOOSER_QUESTIONS",
    "About",
    "AboutKind",
    "AboutReference",
    "ChooserAnswer",
    "ChooserChoice",
    "ChooserQuestion",
    "ChooserQuestionnaire",
    "ChooserResult",
    "DatasetAbout",
    "DatasetAboutUpdate",
    "Difficulty",
    "Recommendation",
    "TypicalRuntime",
    "about_issues",
    "chooser_answers_from_query",
    "describe_dataset",
    "recommend",
]


class Difficulty(StrEnum):
    """How much statistical background an item needs to use well."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    EXPERT = "expert"

    @property
    def rank(self) -> int:
        return _DIFFICULTY_RANK[self]


_DIFFICULTY_RANK: Final = {Difficulty.BEGINNER: 0, Difficulty.INTERMEDIATE: 1, Difficulty.EXPERT: 2}


class TypicalRuntime(StrEnum):
    """Rough wall-clock time on typical data (``S`` resource class)."""

    INSTANT = "instant"
    """Under a second (static checks, small transforms)."""
    SECONDS = "seconds"
    """A few seconds to a minute."""
    MINUTES = "minutes"
    """One to fifteen minutes."""
    LONG = "long"
    """More than fifteen minutes (large Bayesian fits, big searches)."""

    @property
    def rank(self) -> int:
        return list(TypicalRuntime).index(self)


class AboutKind(StrEnum):
    """What an About describes (chooser results and the catalog say which)."""

    MODEL = "model"
    STEP = "step"
    SPLIT = "split"
    TEMPLATE = "template"
    WORKFLOW_TEMPLATE = "workflow_template"
    NODE_KIND = "node_kind"
    NODE = "node"
    CHECK = "check"
    DATASET = "dataset"
    SPEC = "spec"


class AboutReference(StrictModel):
    """A source for the expert detail (a paper, a book chapter, library documentation)."""

    title: str = Field(..., min_length=3, max_length=300)
    url: str | None = Field(None, max_length=500, pattern=r"^https://\S+$")


_ITEMS_MAX = 10


def _items(v: list[str]) -> list[str]:
    for text in v:
        if not 3 <= len(text.strip()) <= 300:
            raise ValueError("each entry must be 3-300 characters")
    if len({t.strip().lower() for t in v}) != len(v):
        raise ValueError("entries must be unique")
    return v


class About(StrictModel):
    """The plain-language card of one catalog item.

    Every field except the expert ones is required, so an About that validates is
    structurally complete; :func:`about_issues` adds the editorial rules (one or two
    sentences, no jargon in the summary, known chooser tags, related ids that exist).
    """

    title: str = Field(..., min_length=3, max_length=80, description="A human name")
    summary_plain: str = Field(
        ...,
        min_length=20,
        max_length=400,
        description="One or two sentences without jargon: what it does for you",
    )
    use_when: list[str] = Field(..., min_length=1, max_length=_ITEMS_MAX)
    avoid_when: list[str] = Field(..., min_length=1, max_length=_ITEMS_MAX)
    data_needs: list[str] = Field(..., min_length=1, max_length=_ITEMS_MAX)
    assumptions: list[str] = Field(..., min_length=1, max_length=_ITEMS_MAX)
    outputs: list[str] = Field(..., min_length=1, max_length=_ITEMS_MAX)
    difficulty: Difficulty
    typical_runtime: TypicalRuntime
    example: str = Field(..., min_length=10, max_length=600, description="A tiny example")
    related: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Ids of related catalog items (models, steps, nodes, checks, templates)",
    )
    references: list[AboutReference] = Field(default_factory=list, max_length=10)
    expert_notes: str | None = Field(
        None, max_length=4000, description="Maths, estimator details, caveats for experts"
    )
    fits: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=16,
        description="Chooser tags: question id -> the answers this item suits",
    )

    model_config = dto_examples(
        {
            "title": "Weekly totals",
            "summary_plain": "Adds up daily numbers into one number per week (or month), so "
            "that every row covers the same length of time.",
            "use_when": ["Your data is daily but you want to plan or report by week"],
            "avoid_when": ["Your data already has one row per week"],
            "data_needs": ["A date column"],
            "assumptions": ["Numbers can be added up (spend, sales); averages are averaged"],
            "outputs": ["The same columns, one row per week"],
            "difficulty": "beginner",
            "typical_runtime": "instant",
            "example": "Daily TV spend of 100 each day becomes 700 for the week.",
            "related": ["core.calendar"],
            "fits": {"goal": ["check_data"]},
        }
    )

    @field_validator("use_when", "avoid_when", "data_needs", "assumptions", "outputs")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _items(v)

    @field_validator("fits")
    @classmethod
    def _fits(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for key, values in v.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) or not values:
                raise ValueError("fits maps a question id to a non-empty list of answers")
            if len(values) > 16 or any(
                not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", x) for x in values
            ):
                raise ValueError("fit answers are short lowercase ids")
        return v

    @field_validator("related")
    @classmethod
    def _related(cls, v: list[str]) -> list[str]:
        for rid in v:
            if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*", rid) or len(rid) > 128:
                raise ValueError(f"related ids are catalog ids, not {rid[:40]!r}")
        if len(set(v)) != len(v):
            raise ValueError("related ids must be unique")
        return v


ABOUT_JARGON: Final[frozenset[str]] = frozenset(
    {
        "autoregressive",
        "autocorrelation",
        "covariance",
        "eigenvalue",
        "heteroskedastic",
        "heteroskedasticity",
        "heteroscedastic",
        "homoskedastic",
        "hyperparameter",
        "hyperprior",
        "posterior",
        "stationarity",
        "stochastic",
        "cointegration",
        "cointegrated",
        "multicollinearity",
        "likelihood",
        "regularisation",
        "regularization",
        "orthogonal",
        "asymptotic",
        "iid",
        "mcmc",
        "nuts",
        "arima",
        "sarimax",
        "varmax",
        "vecm",
    }
)
"""Words that must not appear in a ``summary_plain`` (they may in the other fields, which
explain them, and in ``expert_notes``)."""

_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_WORD = re.compile(r"[a-z]+")


def about_issues(about: About | None, *, known_ids: Iterable[str] | None = None) -> list[str]:
    """Plain-language problems that make an About incomplete (empty list: complete).

    ``None`` is one problem ("no About"). ``known_ids``, when given, are the catalog ids
    ``related`` may point at.
    """
    if about is None:
        return ["the item has no About card"]
    issues: list[str] = []
    sentences = len(_SENTENCE_END.findall(about.summary_plain.strip() + " "))
    if not 1 <= sentences <= 2:
        issues.append("summary_plain must be one or two full sentences")
    jargon = sorted(set(_WORD.findall(about.summary_plain.lower())) & ABOUT_JARGON)
    if jargon:
        issues.append(f"summary_plain uses jargon: {', '.join(jargon)}")
    for key, values in about.fits.items():
        q = _QUESTIONS_BY_ID.get(key)
        if q is None:
            issues.append(f"fits uses an unknown chooser question {key!r}")
            continue
        unknown = sorted(set(values) - {c.value for c in q.choices})
        if unknown:
            issues.append(f"fits[{key!r}] uses unknown answers {unknown}")
    if known_ids is not None:
        known = set(known_ids)
        missing = [r for r in about.related if r not in known]
        if missing:
            issues.append(f"related names unknown items {missing}")
    if about.example.strip().lower() == about.summary_plain.strip().lower():
        issues.append("example must show the item at work, not repeat the summary")
    return issues


# ===================================================================================== datasets


class DatasetAbout(ResponseModel):
    """The About of a dataset: the owner's description plus a generated summary.

    ``summary_generated`` and ``highlights`` come from the version's schema and coordinate
    summary (:func:`describe_dataset`); ``warnings_plain`` from its ingest issues and profile.
    """

    description_user: str | None = Field(None, max_length=4000)
    summary_generated: str = Field(..., max_length=1000)
    highlights: list[str] = Field(default_factory=list, max_length=20)
    warnings_plain: list[str] = Field(default_factory=list, max_length=20)
    generated_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "description_user": "Weekly media spend and revenue from the finance warehouse.",
            "summary_generated": "Weekly data from 2023-01-01 to 2024-12-29 (104 weeks) for "
            "10 markets and 3 channels, with 3 measures: revenue (the result you want to "
            "explain), spend (marketing spend) and price.",
            "highlights": ["104 weeks of history", "10 markets", "3 channels"],
            "warnings_plain": [],
        }
    )


class DatasetAboutUpdate(RequestModel):
    """``PATCH /v1/datasets/{dataset_id}/about``: the owner's own description."""

    description_user: str | None = Field(None, max_length=4000)

    model_config = dto_examples(
        {"description_user": "Weekly media spend and revenue from the finance warehouse."}
    )


_FREQ_UNITS: Final = {"D": "day", "W": "week", "MS": "month", "ME": "month", "QS": "quarter",
                      "QE": "quarter", "YS": "year", "YE": "year", "h": "hour"}  # fmt: skip
_FREQ_ADJ: Final = {"day": "Daily", "week": "Weekly", "month": "Monthly", "quarter": "Quarterly",
                    "year": "Yearly", "hour": "Hourly"}  # fmt: skip
_ROLE_PLAIN: Final = {
    Role.TARGET: "the result you want to explain",
    Role.SPEND: "marketing spend",
    Role.MEDIA: "marketing activity",
    Role.CONTROL: "an outside factor",
    Role.EXPOSURE: "exposure",
    Role.WEIGHT: "a weight",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:?00)?$")


def _plain_time(value: object) -> str:
    """A coordinate label in plain words: ``2023-01-01T00:00:00`` (how ingest summarises
    dates) reads as ``2023-01-01``; other values are unchanged."""
    text = str(value)
    m = _MIDNIGHT.match(text)
    return m.group(1) if m else text


def describe_dataset(
    schema: DataSchema | None,
    summary: DataSummary | None,
    *,
    description: str | None = None,
    warnings: Sequence[str] = (),
) -> DatasetAbout:
    """A plain-language summary of a dataset version from metadata alone (pure; no arrays).

    Example: ``Weekly data from 2023-01-01 to 2024-12-29 (104 weeks) for 10 markets ...``.
    """
    coords = summary.coords if summary is not None else {}
    time_name, time_cs = next(((n, c) for n, c in coords.items() if c.kind == "time"), (None, None))
    parts: list[str] = []
    highlights: list[str] = []
    if time_cs is not None:
        unit = None
        if time_cs.freq:
            base = re.sub(r"^\d+", "", time_cs.freq).split("-")[0]
            unit = _FREQ_UNITS.get(base)
        adj = _FREQ_ADJ.get(unit or "", "Time-stamped")
        span = (
            f" from {_plain_time(time_cs.start)} to {_plain_time(time_cs.end)}"
            if time_cs.start is not None and time_cs.end is not None
            else ""
        )
        count = _plural(time_cs.size, unit or "period")
        parts.append(f"{adj} data{span} ({count})")
        highlights.append(f"{count} of history")
        if not time_cs.contiguous:
            highlights.append(f"some {unit or 'period'}s are missing along {time_name}")
    else:
        parts.append("Data")
    groups = [(n, c) for n, c in coords.items() if n != time_name and c.kind == "categorical"]
    if groups:
        group_text = " and ".join(_plural(c.size, n) for n, c in groups)
        parts.append(f"for {group_text}")
        highlights.extend(_plural(c.size, n) for n, c in groups)
    var_names: list[str] = list(summary.vars) if summary is not None else []
    if schema is not None:
        var_names = list(schema.vars) or var_names
    described: list[str] = []
    for name in var_names[:8]:
        role = schema.vars[name].role if schema is not None and name in schema.vars else None
        plain = _ROLE_PLAIN.get(role) if role is not None else None
        described.append(f"{name} ({plain})" if plain else name)
    sentence = " ".join(parts)
    if var_names:
        more = f" and {len(var_names) - 8} more" if len(var_names) > 8 else ""
        sentence += f", with {_plural(len(var_names), 'measure')}: {', '.join(described)}{more}"
    return DatasetAbout(
        description_user=description,
        summary_generated=(sentence + ".")[:1000],
        highlights=highlights[:20],
        warnings_plain=[w[:300] for w in warnings][:20],
    )


# ====================================================================================== chooser


class ChooserChoice(ResponseModel):
    value: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,31}$")
    label: str = Field(..., max_length=120)
    help_plain: str | None = Field(None, max_length=300)


class ChooserQuestion(ResponseModel):
    """One plain-language question of the chooser (``multiple``: several answers allowed)."""

    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,31}$")
    prompt_plain: str = Field(..., max_length=200)
    help_plain: str | None = Field(None, max_length=400)
    choices: list[ChooserChoice] = Field(..., min_length=2, max_length=16)
    multiple: bool = False
    weight: int = Field(1, ge=1, le=5, description="How much a match on it counts")


class ChooserQuestionnaire(ResponseModel):
    """``GET /v1/catalog/chooser``: the questions, in the order to ask them."""

    questions: list[ChooserQuestion]

    model_config = dto_examples(
        {
            "questions": [
                {
                    "id": "goal",
                    "prompt_plain": "What do you want to do?",
                    "choices": [
                        {"value": "forecast", "label": "Forecast the future"},
                        {"value": "explain_marketing", "label": "Measure what my marketing adds"},
                    ],
                    "multiple": True,
                    "weight": 3,
                }
            ]
        }
    )


def _q(
    id: str,
    prompt: str,
    choices: Sequence[tuple[str, str]],
    *,
    help: str | None = None,
    multiple: bool = False,
    weight: int = 1,
) -> ChooserQuestion:
    return ChooserQuestion(
        id=id,
        prompt_plain=prompt,
        help_plain=help,
        choices=[ChooserChoice(value=v, label=label) for v, label in choices],
        multiple=multiple,
        weight=weight,
    )


CHOOSER_QUESTIONS: Final[tuple[ChooserQuestion, ...]] = (
    _q("goal", "What do you want to do?", [
        ("explain_marketing", "Measure what my marketing adds"),
        ("plan_budget", "Plan or split a marketing budget"),
        ("forecast", "Forecast the future"),
        ("explain_drivers", "Understand what drives a number"),
        ("predict_values", "Predict a value for each store, product or customer"),
        ("understand_choices", "Understand which product people choose"),
        ("find_regimes", "Find periods that behave differently"),
        ("compare_series", "See how several series move together"),
        ("check_data", "Check or prepare my data"),
        ("validate_model", "Check that a model can be trusted"),
    ], weight=3, multiple=True),
    _q("data_shape", "What does your data look like?", [
        ("one_series", "One number over time"),
        ("several_series", "A few related numbers over time"),
        ("panel", "The same numbers for many markets, stores or products"),
        ("choices", "Purchases or choices by customers"),
        ("table", "A table of items without a time order"),
    ], weight=2, multiple=True),
    _q("frequency", "How often is it recorded?", [
        ("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly or less often"),
        ("not_time", "It is not over time"),
    ], multiple=True),
    _q("history", "How much history do you have?", [
        ("short", "Less than a year"), ("medium", "One to three years"),
        ("long", "More than three years"),
    ]),
    _q("drivers", "Do you have other columns that could explain the result?", [
        ("yes", "Yes (prices, promotions, weather, spend)"), ("no", "No, just the number"),
    ], help="Columns you know for the future too (planned prices, booked spend) help forecasts."),
    _q("uncertainty", "Do you need a range (best and worst case), not just one number?", [
        ("essential", "Yes, a range is essential"), ("nice", "It would be nice"),
        ("no", "One number is enough"),
    ]),
    _q("experience", "How comfortable are you with statistics?", [
        ("beginner", "New to it"), ("intermediate", "I know the basics"),
        ("expert", "I am an expert"),
    ]),
    _q("speed", "How quickly do you need results?", [
        ("seconds", "Within seconds"), ("minutes", "Within minutes"),
        ("can_wait", "I can wait"),
    ]),
)  # fmt: skip
"""The chooser's questions (stable ids; :attr:`About.fits` keys and values refer to them)."""

_QUESTIONS_BY_ID: Final = {q.id: q for q in CHOOSER_QUESTIONS}


class ChooserAnswer(RequestModel):
    """One answered question (``values``: one choice, or several for ``multiple``)."""

    question_id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,31}$")
    values: list[str] = Field(..., min_length=1, max_length=16)

    @model_validator(mode="after")
    def _known(self) -> ChooserAnswer:
        q = _QUESTIONS_BY_ID.get(self.question_id)
        if q is None:
            raise ValueError(f"unknown chooser question {self.question_id!r}")
        allowed = {c.value for c in q.choices}
        bad = sorted(set(self.values) - allowed)
        if bad:
            raise ValueError(f"{self.question_id}: unknown answers {bad}")
        if not q.multiple and len(self.values) > 1:
            raise ValueError(f"{self.question_id} takes one answer")
        return self


ChooserItemKind = Literal["model", "template", "workflow_template", "step", "node", "check"]


class Recommendation(ResponseModel):
    """One ranked candidate with the reasons in plain words."""

    kind: AboutKind
    id: str = Field(..., max_length=128)
    title: str
    summary_plain: str
    score: float = Field(..., ge=0, le=1)
    reasons_plain: list[str] = Field(default_factory=list)
    cautions_plain: list[str] = Field(default_factory=list)
    difficulty: Difficulty
    typical_runtime: TypicalRuntime
    about: About | None = None


class ChooserResult(ResponseModel):
    """``GET /v1/catalog/recommendations``: candidates ranked best first."""

    items: list[Recommendation] = Field(default_factory=list)
    answered: list[str] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)

    model_config = dto_examples(
        {
            "items": [
                {
                    "kind": "template",
                    "id": "weekly_forecast",
                    "title": "Weekly forecast",
                    "summary_plain": "Forecasts one weekly number a few months ahead, with "
                    "a range of likely values.",
                    "score": 0.92,
                    "reasons_plain": ["Made for forecasting", "Works on one series"],
                    "cautions_plain": [],
                    "difficulty": "beginner",
                    "typical_runtime": "minutes",
                }
            ],
            "answered": ["goal", "data_shape"],
            "unanswered": ["frequency", "history", "drivers", "uncertainty", "experience",
                           "speed"],
        }
    )  # fmt: skip


def chooser_answers_from_query(query: Mapping[str, Sequence[str]]) -> list[ChooserAnswer]:
    """Answers from the recommendations query string (``?goal=forecast&data_shape=panel``;
    repeat a key or separate with commas for several values). Unknown keys other than
    ``kind`` and ``limit`` are ``CHOOSER_ANSWER_INVALID``."""
    out: list[ChooserAnswer] = []
    for key, raw in query.items():
        if key in ("kind", "limit"):
            continue
        values = [v.strip() for item in raw for v in item.split(",") if v.strip()]
        try:
            out.append(ChooserAnswer(question_id=key, values=values))
        except ValueError as exc:
            raise MapiError(
                f"invalid chooser answer for {key[:40]!r}",
                code=ErrorCode.CHOOSER_ANSWER_INVALID,
                pointer=f"/{key[:40]}",
                untrusted_text=str(exc)[:2000],
            ) from None
    return out


@dataclass(frozen=True, slots=True)
class _Candidate:
    kind: AboutKind
    id: str
    about: About


_SPEED_MAX: Final = {
    "seconds": TypicalRuntime.SECONDS,
    "minutes": TypicalRuntime.MINUTES,
    "can_wait": TypicalRuntime.LONG,
}


def recommend(
    answers: Sequence[ChooserAnswer],
    candidates: Iterable[tuple[AboutKind | str, str, About]],
    *,
    limit: int = 10,
    include_about: bool = False,
    fallback: Iterable[tuple[AboutKind | str, str, About]] | None = None,
) -> ChooserResult:
    """Rank ``candidates`` (``(kind, id, About)``) against ``answers`` (pure, deterministic).

    Each answered question an item declares in :attr:`About.fits` adds the question's weight
    when an answer matches (a reason) or subtracts it when none does (a caution);
    undeclared questions are neutral. ``experience`` below the item's difficulty and
    ``speed`` below its typical runtime cost one point per level. Scores are normalised to
    ``[0, 1]``; ties break by the number of answers an item matches explicitly (an item made
    for every answer before one that is silent on some), then difficulty, then id.

    When ``goal`` is answered, an item made for other goals is never recommended (items
    whose ``goal`` does not match are dropped). When no candidate matches the goal,
    ``fallback`` (other kinds: the building blocks behind the starting points) is ranked
    instead.
    """
    given = {a.question_id: set(a.values) for a in answers}
    exp_rank = (
        min(Difficulty(v).rank for v in given["experience"]) if "experience" in given else None
    )
    speed_cap = min(_SPEED_MAX[v].rank for v in given["speed"]) if "speed" in given else None
    scored: list[tuple[float, int, int, str, Recommendation, bool, bool]] = []
    for kind, cid, about in candidates:
        points, total = 0.0, 0.0
        reasons: list[str] = []
        cautions: list[str] = []
        goal_match = False
        for qid, values in given.items():
            q = _QUESTIONS_BY_ID.get(qid)
            if q is None or qid in ("experience", "speed"):
                continue
            fit = about.fits.get(qid)
            if fit is None:
                continue
            total += q.weight
            labels = {c.value: c.label for c in q.choices}
            hit = sorted(values & set(fit))
            if hit:
                points += q.weight
                reasons.append(f"Suits: {labels[hit[0]].lower()}")
                goal_match = goal_match or qid == "goal"
            else:
                points -= q.weight
                cautions.append(f"Not made for: {labels[sorted(values)[0]].lower()}")
        if exp_rank is not None:
            total += 1
            gap = about.difficulty.rank - exp_rank
            if gap > 0:
                points -= gap
                cautions.append(f"Needs {about.difficulty.value}-level statistics knowledge")
            else:
                points += 1
        if speed_cap is not None:
            total += 1
            gap = about.typical_runtime.rank - speed_cap
            if gap > 0:
                points -= gap
                cautions.append(f"Usually takes {about.typical_runtime.value}")
            else:
                points += 1
        score = 0.5 if total == 0 else max(0.0, min(1.0, 0.5 + points / (2 * total)))
        rec = Recommendation(
            kind=AboutKind(kind),
            id=cid,
            title=about.title,
            summary_plain=about.summary_plain,
            score=round(score, 4),
            reasons_plain=reasons,
            cautions_plain=cautions,
            difficulty=about.difficulty,
            typical_runtime=about.typical_runtime,
            about=about if include_about else None,
        )
        silent = "goal" not in about.fits  # made neither for the goal nor for another one
        scored.append((-score, -len(reasons), about.difficulty.rank, cid, rec, goal_match, silent))
    if "goal" in given:
        if fallback is not None and not any(s[5] for s in scored):
            return recommend(answers, fallback, limit=limit, include_about=include_about)
        scored = [s for s in scored if s[5] or s[6]]
    scored.sort(key=lambda s: (s[0], s[1], s[2], s[3]))
    return ChooserResult(
        items=[s[4] for s in scored[: max(0, limit)]],
        answered=[q.id for q in CHOOSER_QUESTIONS if q.id in given],
        unanswered=[q.id for q in CHOOSER_QUESTIONS if q.id not in given],
    )
