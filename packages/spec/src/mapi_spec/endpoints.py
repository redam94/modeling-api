"""Endpoints: stable aliases for promoted model versions (plan §2, §8.2, §11; Q13; M3).

An endpoint ``endpoint://<project>/<alias>`` points at one model version at a time.
Integrations (the Python and TypeScript clients, Google Sheets, BI exports) call the
alias, so a new model version can be promoted - or rolled back - without touching them.

* **Promotion** (``PUT /v1/endpoints/{alias}``) passes the promotion gate
  (:func:`mapi_spec.scorecard.promotion_gate`): the scorecard, the endpoint's
  :class:`EvaluationPolicy` (``evaluate_then_refit`` by default, Q13) and the input-schema
  compatibility with what callers already send (:func:`schema_breaks`).
* Every promotion and rollback is an :class:`EndpointRevision` (the history).
* **Scoring** (``POST /v1/endpoints/{alias}/score``) takes an :class:`EndpointScoreCall`:
  a score request without ``apiVersion``/``kind``/``model``, whose ``data`` and ``scenario``
  may be dataset references *or* small inline XrJSON datasets.
* ``GET …/schema`` describes the input (:class:`EndpointSchema`, with an example payload),
  ``POST …/validate`` checks a call without running it, ``GET …/snippets`` returns code
  (:mod:`mapi_spec.snippets`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

from pydantic import AwareDatetime, Field, JsonValue, field_serializer, model_validator

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, Sha256, TenantId
from mapi_spec._dto import dto_examples as _examples
from mapi_spec._types import Duration, LabelKey, LabelValue, Name, Scalar, Slug
from mapi_spec.capabilities import OutputSelector, Representation, ScoreMode, default_summary
from mapi_spec.coords import CoordSummary, parse_freq
from mapi_spec.errors import Issue
from mapi_spec.refs import DatasetRef
from mapi_spec.resources import ResourceClass
from mapi_spec.schema import DataSchema, DimPolicy, Role
from mapi_spec.scorecard import PromotionGateDecision
from mapi_spec.spec import (
    API_VERSION,
    FitMode,
    FutureCovariates,
    Horizon,
    Intervention,
    ScoreRequest,
)
from mapi_spec.xrjson import XrJsonDataset, observed_labels, xrjson_from_rows

__all__ = [
    "INPUT_INLINE_DATA",
    "INPUT_INLINE_SCENARIO",
    "Endpoint",
    "EndpointPromote",
    "EndpointRevision",
    "EndpointRollback",
    "EndpointSchema",
    "EndpointSchemaSnapshot",
    "EndpointScoreCall",
    "EndpointValidation",
    "EvaluationPolicy",
    "InputColumnRole",
    "InputTable",
    "InputTableColumn",
    "ModelInputSchema",
    "capability_breaks",
    "endpoint_ref",
    "example_actuals_call",
    "example_score_call",
    "input_table",
    "policy_relaxations",
    "schema_breaks",
    "table_forecast_call",
]

INPUT_INLINE_DATA: Final = "inline.data"
INPUT_INLINE_SCENARIO: Final = "inline.scenario"
"""Run input names under which the server stores inline XrJSON payloads of a score call
(the runner decodes them in the sandbox; see :meth:`EndpointScoreCall.inline_inputs`)."""

_MV = "mv_01jaf3e1f2g3h4j5k6m7n8p9q0"
_MDL = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0"
_USR = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"


def endpoint_ref(project: str, alias: str) -> str:
    """``endpoint://<project>/<alias>``."""
    return f"endpoint://{project}/{alias}"


class EvaluationPolicy(RequestModel):
    """What a model version must satisfy to be promoted to the endpoint (Q13).

    ``fit_mode = evaluate_then_refit`` (default) requires versions whose production bundle
    was refitted on all data after the holdout evaluation; ``train_only`` also accepts
    exploration fits. ``schema_change`` decides what happens when the new version expects
    input the endpoint's callers do not send (:func:`schema_breaks`).
    """

    fit_mode: FitMode = FitMode.EVALUATE_THEN_REFIT
    schema_change: Literal["block", "warn", "allow"] = "block"

    model_config = _examples({"fit_mode": "evaluate_then_refit", "schema_change": "block"})


class EndpointSchemaSnapshot(ResponseModel):
    """The input contract of the promoted version, frozen at promotion: what callers send.

    ``input_schema`` is the schema at ``inputs_stage`` under the spec's canonical names
    (``data.bind`` applied: ``time``, ``y``), which inline payloads use; the runner renames
    them to the dataset's own names before scoring. ``horizon_dim`` is the canonical name of
    the dim a forecast extends. ``conditions_on_actuals``: forecasts can carry actuals
    (target values for periods after the fit) that the model updates its state with
    (:mod:`mapi_spec.actuals`); the server sets it from the capabilities of the plugin
    build that serves the endpoint now, not only from those at fit.
    """

    inputs_stage: str = "raw"
    input_schema: DataSchema
    schema_hash: Sha256
    score_modes: list[ScoreMode] = Field(default_factory=list)
    representation: Representation | None = None
    horizon_dim: Name | None = None
    future_required: list[str] = Field(default_factory=list)
    fit_coords: dict[str, CoordSummary] = Field(default_factory=dict)
    conditions_on_actuals: bool = False

    model_config = _examples(
        {
            "inputs_stage": "raw",
            "input_schema": {
                "dims": {
                    "market": {"kind": "categorical", "policy": "new_levels",
                               "new_level_strategy": "pooled"},
                    "channel": {"kind": "categorical"},
                    "date": {"kind": "time", "policy": "extend", "freq": "D"},
                },
                "vars": {
                    "revenue_local": {"dims": ["market", "date"], "dtype": "float64",
                                      "role": "target"},
                    "spend_local": {"dims": ["market", "channel", "date"], "dtype": "float64",
                                    "role": "spend", "future_required": True},
                },
            },
            "schema_hash": "sha256:" + "5a" * 32,
            "score_modes": ["counterfactual", "evaluate", "forecast", "predict"],
            "representation": "draws",
            "horizon_dim": "time",
            "future_required": ["spend_local"],
            "fit_coords": {
                "market": {"kind": "categorical", "size": 2, "values": ["de", "fr"]},
                "channel": {"kind": "categorical", "size": 2, "values": ["search", "tv"]},
                "date": {"kind": "time", "size": 730, "start": "2024-01-01",
                         "end": "2025-12-30", "freq": "D"},
            },
        }
    )  # fmt: skip


class Endpoint(ResponseModel):
    """A stable alias and the model version it serves now."""

    id: Id = Field(..., pattern=r"^ep_[0-9a-z]{26}$")
    alias: Slug
    tenant_id: TenantId
    project_id: TenantId
    ref: str = Field(..., description="endpoint://project/alias")
    description: str | None = Field(None, max_length=2000)
    model_id: Id
    model_version_id: Id
    model_ref: str = Field(..., description="model://project/name@vN")
    revision: int = Field(..., ge=1, description="Increases with every promotion and rollback")
    evaluation_policy: EvaluationPolicy = Field(default_factory=EvaluationPolicy)
    schema_snapshot: EndpointSchemaSnapshot
    gate: PromotionGateDecision
    overridden: bool = Field(False, description="The current version was promoted by override")
    promoted_by: Id | None = None
    promoted_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime

    model_config = _examples(
        {
            "id": "ep_01jaf3g1h2j3k4m5n6p7q8r9s0",
            "alias": "geo-mmm-prod",
            "tenant_id": "acme",
            "project_id": "growth",
            "ref": "endpoint://growth/geo-mmm-prod",
            "description": "Weekly EU MMM used by the budget planner",
            "model_id": _MDL,
            "model_version_id": _MV,
            "model_ref": "model://growth/eu-geo-mmm@v3",
            "revision": 4,
            "evaluation_policy": {"fit_mode": "evaluate_then_refit", "schema_change": "block"},
            "schema_snapshot": EndpointSchemaSnapshot.model_json_schema()["examples"][0],
            "gate": {"outcome": "allowed", "scorecard_overall": "pass"},
            "overridden": False,
            "promoted_by": _USR,
            "promoted_at": EXAMPLE_TS,
            "created_at": "2026-09-01T08:00:00Z",
            "updated_at": EXAMPLE_TS,
        }
    )


class EndpointPromote(RequestModel):
    """``PUT /v1/endpoints/{alias}``: create the alias or point it at another version.

    Name the version by id or by ``model://project/name@vN`` (exactly one). A version whose
    gate says ``needs_override`` needs ``override=true`` and a ``reason`` from a signed-in
    project admin (audited); ``blocked`` versions cannot be promoted. ``expected_revision``
    guards against concurrent promotions (``409 CONFLICT`` when stale).
    """

    model_version_id: Id | None = None
    model: str | None = Field(None, pattern=r"^model://[a-z0-9_-]+/[a-z0-9_-]+@v[0-9]+$")
    override: bool = False
    reason: str | None = Field(None, max_length=1000)
    evaluation_policy: EvaluationPolicy | None = None
    description: str | None = Field(None, max_length=2000)
    expected_revision: int | None = Field(None, ge=0, description="0: the alias must be new")

    model_config = _examples(
        {"model": "model://growth/eu-geo-mmm@v3", "expected_revision": 3},
        {
            "model_version_id": _MV,
            "override": True,
            "reason": "Holdout MAPE is high because of the strike in week 41; accepted by CMO.",
        },
    )

    @model_validator(mode="after")
    def _one(self) -> EndpointPromote:
        if (self.model_version_id is None) == (self.model is None):
            raise ValueError("give exactly one of 'model_version_id' or 'model'")
        if self.override and not (self.reason and self.reason.strip()):
            raise ValueError("an override needs a reason")
        return self


class EndpointRevision(ResponseModel):
    """One entry of an endpoint's history (newest first in ``GET …/history``)."""

    alias: Slug
    revision: int = Field(..., ge=1)
    action: Literal["promote", "rollback"]
    model_version_id: Id
    model_ref: str
    previous_model_version_id: Id | None = None
    gate: PromotionGateDecision
    overridden: bool = False
    reason: str | None = Field(None, max_length=1000)
    actor_id: str | None = Field(None, max_length=128)
    token_id: Id | None = None
    created_at: AwareDatetime

    model_config = _examples(
        {
            "alias": "geo-mmm-prod",
            "revision": 4,
            "action": "promote",
            "model_version_id": _MV,
            "model_ref": "model://growth/eu-geo-mmm@v3",
            "previous_model_version_id": "mv_01jaf3e0f0g0h0j0k0m0n0p0q0",
            "gate": PromotionGateDecision.model_json_schema()["examples"][0],
            "overridden": True,
            "reason": "Holdout MAPE is high because of the strike in week 41.",
            "actor_id": _USR,
            "created_at": EXAMPLE_TS,
        }
    )


class EndpointRollback(RequestModel):
    """``POST /v1/endpoints/{alias}/rollback``: point the alias back at the version of an
    earlier revision (default: the one before the current). The gate is not re-run - the
    version was promoted before - but the rollback is audited with its reason. M3.5: when a
    governance policy requires validated models, the earlier version must still be
    ``validated`` (``NOT_VALIDATED`` otherwise)."""

    to_revision: int | None = Field(None, ge=1)
    reason: str = Field(..., min_length=1, max_length=1000)

    model_config = _examples({"reason": "v3 over-forecasts after the price change"})


class EndpointScoreCall(RequestModel):
    """``POST /v1/endpoints/{alias}/score`` (and ``…/validate``): a score request for the
    endpoint's current version.

    ``data``/``scenario`` are ``dataset://`` references or inline XrJSON datasets (up to
    5 MB; :mod:`mapi_spec.xrjson`). Send ``Prefer: wait=N`` (N ≤ 60) to wait: 200 with the
    result when the run finishes in time, else 202 with the run.
    """

    mode: ScoreMode = "predict"
    horizon: Horizon | None = None
    inputs_stage: str = Field("raw", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    data: DatasetRef | XrJsonDataset | None = None
    scenario: DatasetRef | XrJsonDataset | None = None
    side_tables: dict[Name, DatasetRef] = Field(default_factory=dict, max_length=32)
    select: dict[Name, list[Scalar]] = Field(default_factory=dict, max_length=64)
    interventions: list[Intervention] = Field(default_factory=list, max_length=256)
    output: OutputSelector = Field(default_factory=OutputSelector)
    seed: int | None = Field(None, ge=0, lt=2**32)
    future_covariates: FutureCovariates = FutureCovariates.SUPPLIED
    resource_class: ResourceClass | None = None
    timeout: Duration | None = None
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=64)

    model_config = _examples(
        {
            "mode": "forecast",
            "horizon": {"dim": "time", "periods": 8},
            "future_covariates": "carry_last",
            "output": {"kind": "expected", "summary": ["mean", "hdi_90"]},
        },
        {
            "mode": "predict",
            "data": {
                "coords": {
                    "market": {"dims": ["market"], "data": ["de"]},
                    "channel": {"dims": ["channel"], "data": ["search", "tv"]},
                    "date": {"dims": ["date"], "data": ["2025-12-30"]},
                },
                "data_vars": {
                    "spend_local": {
                        "dims": ["market", "channel", "date"],
                        "data": [[[1200.0], [5300.0]]],
                    }
                },
            },
        },
    )

    @model_validator(mode="after")
    def _modes(self) -> EndpointScoreCall:
        if self.mode in ("filter", "evaluate") and not isinstance(self.data, DatasetRef):
            raise ValueError(
                f"mode {self.mode!r} needs data as a dataset:// reference (upload it first)"
            )
        return self

    def to_score_request(self, endpoint: str) -> ScoreRequest:
        """The :class:`~mapi_spec.spec.ScoreRequest` for ``endpoint`` (an
        ``endpoint://`` ref). Inline datasets are left out: the server stores them as the
        run inputs :data:`INPUT_INLINE_DATA` / :data:`INPUT_INLINE_SCENARIO`."""
        body = self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"data", "scenario", "resource_class", "timeout", "labels"},
            exclude_defaults=True,
        )
        body.update({"apiVersion": API_VERSION, "kind": "ScoreRequest", "model": endpoint})
        for name in ("data", "scenario"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, XrJsonDataset):
                body[name] = str(value)
        return ScoreRequest.model_validate(body)

    @field_serializer("data", "scenario")
    def _ser_data(self, value: str | XrJsonDataset | None) -> object:
        # a union with a string-serialised ref: serialise each member explicitly
        if isinstance(value, XrJsonDataset):
            return value.model_dump(mode="json", by_alias=True)
        return None if value is None else str(value)

    def inline_inputs(self) -> dict[str, XrJsonDataset]:
        """Inline payloads by run input name."""
        out: dict[str, XrJsonDataset] = {}
        if isinstance(self.data, XrJsonDataset):
            out[INPUT_INLINE_DATA] = self.data
        if isinstance(self.scenario, XrJsonDataset):
            out[INPUT_INLINE_SCENARIO] = self.scenario
        return out


InputColumnRole = Literal["coord", "target", "driver", "input"]
"""What an input-table column holds: ``coord`` - the labels of a dim (a date, a market);
``target`` - the KPI (in a forecast: an actual, or empty for a period to forecast);
``driver`` - an input the model needs on the periods it forecasts; ``input`` - any other."""


class InputTableColumn(ResponseModel):
    """One column of an :class:`InputTable`."""

    name: Name
    role: InputColumnRole
    dims: list[Name] = Field(
        default_factory=list, description="The dims a value column varies along"
    )
    freq: str | None = Field(None, description="The frequency of a time coordinate")
    description: str = Field("", max_length=500)


class InputTable(ResponseModel):
    """An endpoint's inline input as one long table: a header row (:attr:`columns`, the
    coordinate columns first), then one row per coordinate - the layout of a spreadsheet's
    Input sheet and of :func:`mapi_spec.xrjson.xrjson_from_rows`.

    For a forecasting endpoint :attr:`rows` lists the periods after the fitted history (the
    other dims' first levels), every value empty: fill the target where you have actuals and
    leave it empty for the periods to forecast (:mod:`mapi_spec.actuals`). Dates are ISO
    ``yyyy-MM-dd``."""

    horizon_dim: Name | None = None
    conditions_on_actuals: bool = False
    columns: list[InputTableColumn] = Field(default_factory=list, max_length=300)
    rows: list[list[Scalar | None]] = Field(default_factory=list, max_length=200)

    model_config = _examples(
        {
            "horizon_dim": "t",
            "conditions_on_actuals": True,
            "columns": [
                {"name": "t", "role": "coord", "freq": "MS",
                 "description": "The period (yyyy-MM-dd)"},
                {"name": "y", "role": "target", "dims": ["t"],
                 "description": "The actual; leave empty for the periods to forecast"},
                {"name": "price", "role": "driver", "dims": ["t"],
                 "description": "Needed on every period, the periods to forecast included"},
            ],
            "rows": [["1961-01-01", None, None], ["1961-02-01", None, None]],
        }
    )  # fmt: skip

    @property
    def header(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def variables(self) -> dict[str, list[str]]:
        """Value columns and their dims (the input of ``xrjson_from_rows``)."""
        return {c.name: list(c.dims) for c in self.columns if c.role != "coord"}


class ModelInputSchema(ResponseModel):
    """``GET /v1/models/{model_id}/versions/{version}/inputs``: what a score call of one model
    version can send - the same input contract an endpoint serving the version would freeze
    (:func:`~mapi_server.services.endpoints.snapshot_for`) and its input table.

    The names are the spec's canonical ones (``data.bind`` applied), which inline payloads
    (``ScoreCall.inline``) use. ``fit_coords`` tell a caller what the model saw: the fitted
    periods and every level of the other dims."""

    model_ref: str
    snapshot: EndpointSchemaSnapshot
    input_table: InputTable | None = Field(
        None, description="The inline input as a long table, with the periods after the fit"
    )
    max_inline_bytes: int = 5 * 1024 * 1024

    model_config = _examples(
        {
            "model_ref": "model://growth/eu-geo-mmm@v3",
            "snapshot": EndpointSchemaSnapshot.model_json_schema()["examples"][0],
            "input_table": InputTable.model_json_schema()["examples"][0],
        }
    )


class EndpointSchema(ResponseModel):
    """``GET /v1/endpoints/{alias}/schema``: what to send, with a ready example payload."""

    alias: Slug
    ref: str
    model_ref: str
    revision: int = Field(..., ge=1)
    snapshot: EndpointSchemaSnapshot
    example: dict[str, JsonValue] = Field(
        ..., description="An EndpointScoreCall body that validates against this endpoint"
    )
    actuals_example: dict[str, JsonValue] | None = Field(
        None,
        description="For endpoints whose model conditions forecasts on actuals: a forecast "
        "call with inline data over the periods after the fit, the target empty (null). Fill "
        "the target with the actuals you have and leave it empty for the periods to forecast "
        "(example_actuals_call)",
    )
    input_table: InputTable | None = Field(
        None,
        description="The inline input as a long table (a spreadsheet's Input sheet): the "
        "columns and, for forecasts, the periods after the fit to fill in (input_table)",
    )
    max_inline_bytes: int = 5 * 1024 * 1024

    model_config = _examples(
        {
            "alias": "geo-mmm-prod",
            "ref": "endpoint://growth/geo-mmm-prod",
            "model_ref": "model://growth/eu-geo-mmm@v3",
            "revision": 4,
            "snapshot": EndpointSchemaSnapshot.model_json_schema()["examples"][0],
            "example": EndpointScoreCall.model_json_schema()["examples"][0],
        }
    )


class EndpointValidation(ResponseModel):
    """``POST /v1/endpoints/{alias}/validate``: the static checks of a score call (CoordPlan,
    capabilities, inline payload envelope) without running it."""

    valid: bool
    issues: list[Issue] = Field(default_factory=list)
    request: ScoreRequest | None = None

    model_config = _examples(
        {
            "valid": False,
            "issues": [
                {
                    "code": "MISSING_FUTURE_COVARIATE",
                    "severity": "error",
                    "pointer": "/scenario",
                    "message": "forecasting needs future values of ['spend'] at stage 'raw'",
                    "suggested_action": "Provide them in a scenario dataset.",
                }
            ],
        }
    )


# ================================================================================ compatibility

_SCHEMA_CHANGE_RANK: Final = {"block": 0, "warn": 1, "allow": 2}


def policy_relaxations(old: EvaluationPolicy, new: EvaluationPolicy) -> list[str]:
    """How ``new`` lets through what ``old`` stops (plain text; empty: same or stricter).

    Relaxing a policy disarms the promotion gate as an override would, so the server lets
    only those who may override (a signed-in endpoint admin, never a token) do it.
    """
    out: list[str] = []
    if _SCHEMA_CHANGE_RANK[new.schema_change] > _SCHEMA_CHANGE_RANK[old.schema_change]:
        out.append(f"schema_change {old.schema_change} -> {new.schema_change}")
    if old.fit_mode is FitMode.EVALUATE_THEN_REFIT and new.fit_mode is FitMode.TRAIN_ONLY:
        out.append(f"fit_mode {old.fit_mode.value} -> {new.fit_mode.value}")
    return out


_FAMILY = {"float64": "num", "float32": "num", "int64": "num", "int32": "num", "bool": "bool",
           "str": "str", "datetime64[ns]": "time"}  # fmt: skip


def schema_breaks(old: DataSchema, new: DataSchema) -> list[str]:
    """Why callers that send data for ``old`` would break against ``new`` (plain text).

    Breaking: a dim that disappears or changes kind; a dim policy that no longer allows
    what it did (``extend``/``new_levels`` → ``fixed``); a new required input variable (not
    a target, missing values forbidden); an input variable whose dims or dtype family
    change. Dropped variables and loosened policies are compatible.
    """
    out: list[str] = []
    for name, dim in old.dims.items():
        nd = new.dims.get(name)
        if nd is None:
            out.append(f"dim {name!r} is gone")
            continue
        if nd.kind != dim.kind:
            out.append(f"dim {name!r} changed from {dim.kind} to {nd.kind}")
        if dim.policy is not DimPolicy.FIXED and nd.policy is not dim.policy:
            # fixed allows nothing new; extend and new_levels each allow one thing
            out.append(f"dim {name!r} policy changed from {dim.policy.value} to {nd.policy.value}")
    for name, var in new.vars.items():
        if var.role is Role.TARGET:
            continue
        ov = old.vars.get(name)
        if ov is None:
            if var.missing == "forbid":
                out.append(f"new required input {name!r}")
            continue
        if tuple(ov.dims) != tuple(var.dims):
            out.append(f"input {name!r} now has dims {list(var.dims)} (was {list(ov.dims)})")
        if _FAMILY.get(ov.dtype) != _FAMILY.get(var.dtype):
            out.append(f"input {name!r} changed type from {ov.dtype} to {var.dtype}")
    return out


def capability_breaks(old: EndpointSchemaSnapshot, new: EndpointSchemaSnapshot) -> list[str]:
    """Why callers of ``old`` would break against ``new`` beyond the input schema (plain
    text): a version that forecasts but cannot update the forecast with actuals refuses calls
    that send them (``ACTUALS_NOT_SUPPORTED``, :mod:`mapi_spec.actuals`), which the version
    served now accepts. Counted with :func:`schema_breaks` by the endpoint's
    ``schema_change`` policy."""
    out: list[str] = []
    if (
        old.conditions_on_actuals
        and "forecast" in old.score_modes
        and not new.conditions_on_actuals
        and "forecast" in new.score_modes
    ):
        out.append(
            "forecasts with actuals: the version served now updates a forecast with the "
            "actuals a call sends; this version refuses them (ACTUALS_NOT_SUPPORTED)"
        )
    return out


def example_score_call(snapshot: EndpointSchemaSnapshot, *, periods: int = 8) -> EndpointScoreCall:
    """An example payload for an endpoint (shown by ``GET …/schema`` and the snippets).

    Forecasting endpoints get a forecast of ``periods`` that carries covariates forward
    (no inline data needed); others get a ``predict`` call with a one-cell-per-level inline
    dataset over the last fitted period, with placeholder zeros for every input variable.
    The summary is the model's representation default (``mean``/``hdi_90`` for draws,
    ``median``/``q5``/``q95`` for quantile models, which have no mean), so the example scores.
    """
    rep = snapshot.representation
    output = OutputSelector(
        kind="expected",
        summary=default_summary(rep) if rep is not None else ["mean", "hdi_90"],
    )
    if "forecast" in snapshot.score_modes and snapshot.horizon_dim is not None:
        return EndpointScoreCall(
            mode="forecast",
            horizon=Horizon(dim=snapshot.horizon_dim, periods=periods),
            future_covariates=FutureCovariates.CARRY_LAST,
            output=output,
        )
    schema = snapshot.input_schema
    coords: dict[str, dict[str, object]] = {}
    sizes: dict[str, int] = {}
    for name, dim in schema.dims.items():
        cs = snapshot.fit_coords.get(name)
        if cs is not None and cs.values:
            values: list[Scalar] = list(cs.values[:2])
        elif cs is not None and cs.end is not None:
            values = [cs.end]
        elif dim.kind in ("integer", "index"):
            values = [0]
        else:
            values = ["example"]
        coords[name] = {"dims": [name], "data": values}
        sizes[name] = len(values)
    data_vars: dict[str, dict[str, object]] = {}
    for name, var in schema.vars.items():
        if var.role in (Role.TARGET, Role.INDEX, Role.CONSTANT):
            continue
        data_vars[name] = {"dims": list(var.dims), "data": _zeros([sizes[d] for d in var.dims])}
    if not data_vars:
        return EndpointScoreCall(mode="predict", output=output)
    payload = XrJsonDataset.model_validate({"coords": coords, "data_vars": data_vars})
    return EndpointScoreCall(mode="predict", data=payload, output=output)


def example_actuals_call(
    snapshot: EndpointSchemaSnapshot, *, periods: int = 8
) -> EndpointScoreCall | None:
    """A forecast call that shows how to send actuals (``EndpointSchema.actuals_example``):
    inline data over the ``periods`` periods after the fit with every input variable empty
    (``null``) - the target and the drivers - and ``future_covariates: carry_last``.

    As it stands it forecasts those periods from the end of the fit (no actuals). A caller
    (a spreadsheet's Input sheet, a script) fills the target for the periods it has actuals
    for and leaves it empty for the periods to forecast; driver values it leaves empty on the
    future periods are carried forward (:mod:`mapi_spec.actuals`). ``None`` unless the
    endpoint forecasts with a model that conditions on actuals.
    """
    hdim = snapshot.horizon_dim
    schema = snapshot.input_schema
    if (
        not snapshot.conditions_on_actuals
        or "forecast" not in snapshot.score_modes
        or hdim is None
        or hdim not in schema.dims
        or not schema.targets
    ):
        return None
    fit = snapshot.fit_coords.get(hdim)
    labels = _future_labels(fit, periods) if fit is not None else None
    if labels is None:
        return None
    coords: dict[str, dict[str, object]] = {hdim: {"dims": [hdim], "data": labels}}
    sizes: dict[str, int] = {hdim: len(labels)}
    data_vars: dict[str, dict[str, object]] = {}
    for name, var in schema.vars.items():
        if var.role in (Role.INDEX, Role.CONSTANT) or hdim not in var.dims:
            continue
        for d in var.dims:
            if d in sizes:
                continue
            cs = snapshot.fit_coords.get(d)
            values: list[Scalar] = list(cs.values[:2]) if cs is not None and cs.values else []
            if not values:
                return None  # a dim whose levels are unknown: no honest example
            coords[d] = {"dims": [d], "data": values}
            sizes[d] = len(values)
        data_vars[name] = {"dims": list(var.dims), "data": _nulls([sizes[d] for d in var.dims])}
    rep = snapshot.representation
    output = OutputSelector(
        kind="expected",
        summary=default_summary(rep) if rep is not None else ["mean", "hdi_90"],
    )
    payload = XrJsonDataset.model_validate({"coords": coords, "data_vars": data_vars})
    return EndpointScoreCall(
        mode="forecast",
        horizon=Horizon(dim=hdim, periods=periods),
        data=payload,
        future_covariates=FutureCovariates.CARRY_LAST,
        output=output,
    )


def _future_labels(fit: CoordSummary, periods: int) -> list[Scalar] | None:
    """The ``periods`` labels after the end of ``fit`` (ISO dates or integers)."""
    if fit.end is None:
        return None
    if fit.kind == "time":
        if fit.freq is None:
            return None
        freq = parse_freq(fit.freq)
        last = fit.ordinal(fit.end)
        if last is None:
            return None
        out: list[Scalar] = []
        for k in range(1, periods + 1):
            dt = freq.label(last + k)
            midnight = dt.hour == dt.minute == dt.second == 0
            out.append(dt.date().isoformat() if midnight else dt.isoformat())
        return out
    if fit.kind in ("integer", "index") and isinstance(fit.end, int):
        step = int(fit.step or 1)
        return [fit.end + k * step for k in range(1, periods + 1)]
    return None


def _nulls(shape: list[int]) -> object:
    if not shape:
        return None
    return [_nulls(shape[1:]) for _ in range(shape[0])]


_SKIP_ROLES: Final = (Role.INDEX, Role.CONSTANT)
_MAX_TABLE_ROWS: Final = 200


def input_table(snapshot: EndpointSchemaSnapshot, *, periods: int = 8) -> InputTable | None:
    """The endpoint's inline input as a long table (``EndpointSchema.input_table``).

    Columns: the dims the input variables use (the forecast dim first), then the targets,
    the drivers needed on future periods and the other inputs. For a forecasting endpoint the
    example rows are the ``periods`` periods after the fit (crossed with the first two levels
    of every other dim), every value empty - the rows a caller fills with actuals or leaves
    empty to forecast. ``None`` when the input has no variable a table could hold.
    """
    schema = snapshot.input_schema
    hdim = snapshot.horizon_dim if snapshot.horizon_dim in schema.dims else None
    names = [n for n, v in schema.vars.items() if v.role not in _SKIP_ROLES]
    if not names:
        return None
    future = set(snapshot.future_required) | {
        n for n, v in schema.vars.items() if v.future_required
    }
    targets = [n for n in names if schema.vars[n].role is Role.TARGET]
    drivers = [n for n in names if n in future and n not in targets]
    others = [n for n in names if n not in targets and n not in drivers]
    used = [d for d in schema.dims if any(d in schema.vars[n].dims for n in names)]
    if hdim is not None and hdim in used:
        used.remove(hdim)
        used.insert(0, hdim)
    forecasting = hdim is not None and "forecast" in snapshot.score_modes
    columns: list[InputTableColumn] = []
    for d in used:
        dim = schema.dims[d]
        if dim.kind == "time":
            text = "The period (yyyy-MM-dd)" if d == hdim else "A date (yyyy-MM-dd)"
        else:
            text = f"A level of {d}"
        columns.append(InputTableColumn(name=d, role="coord", freq=dim.freq, description=text))
    for n in targets:
        text = "The value of the target"
        if forecasting:
            text = (
                "The actual; leave empty for the periods to forecast"
                if snapshot.conditions_on_actuals
                else "Leave empty: this model forecasts from the end of its fitted history"
            )
        columns.append(
            InputTableColumn(
                name=n, role="target", dims=list(schema.vars[n].dims), description=text
            )
        )
    for n in drivers:
        columns.append(
            InputTableColumn(
                name=n,
                role="driver",
                dims=list(schema.vars[n].dims),
                description="Needed on every period, the periods to forecast included",
            )
        )
    for n in others:
        columns.append(
            InputTableColumn(
                name=n, role="input", dims=list(schema.vars[n].dims), description="An input"
            )
        )
    rows: list[list[Scalar | None]] = []
    fit = snapshot.fit_coords.get(hdim) if forecasting and hdim is not None else None
    labels = _future_labels(fit, periods) if fit is not None else None
    if labels and hdim in used:
        levels: list[list[Scalar]] = [labels]
        for d in used[1:]:
            cs = snapshot.fit_coords.get(d)
            if cs is None or not cs.values:
                levels = []
                break
            levels.append(list(cs.values[:2]))
        combos: list[list[Scalar]] = [[]]
        for values in levels:
            combos = [[*c, v] for c in combos for v in values][:_MAX_TABLE_ROWS]
        if levels:
            empty: list[Scalar | None] = [None] * (len(columns) - len(used))
            rows = [[*c, *empty] for c in combos]
    return InputTable(
        horizon_dim=hdim,
        conditions_on_actuals=snapshot.conditions_on_actuals,
        columns=columns,
        rows=rows,
    )


def table_forecast_call(
    table: InputTable,
    rows: Iterable[Mapping[str, Any]],
    *,
    periods: int | None = None,
    future_covariates: FutureCovariates | str = FutureCovariates.CARRY_LAST,
    output: OutputSelector | Mapping[str, Any] | None = None,
) -> EndpointScoreCall:
    """A forecast call over a long table in the layout of ``table`` (a spreadsheet's Input
    sheet, a DataFrame's records): rows whose target holds a value are actuals, rows after
    the last actual with the target empty are the periods to forecast
    (:mod:`mapi_spec.actuals`).

    ``horizon.periods`` is the number of those periods; when the table ends with an actual
    it is ``periods`` (default 8) periods appended after it. Raises ``ValueError`` when the
    endpoint does not forecast or the table does not fit the layout.
    """
    hdim = table.horizon_dim
    if hdim is None:
        raise ValueError("this endpoint has no dim a forecast extends")
    data = xrjson_from_rows(rows, table.variables)
    coord = data.coords.get(hdim)
    if coord is None:
        raise ValueError(f"the table needs a column named {hdim!r} (the periods)")
    labels = list(coord.data) if isinstance(coord.data, list) else []
    order = list(range(len(labels)))
    with contextlib.suppress(TypeError):
        order.sort(key=lambda i: labels[i])  # type: ignore[arg-type,return-value]
    observed: set[Any] = set()
    for target in (c.name for c in table.columns if c.role == "target"):
        found = observed_labels(data, target, hdim)
        observed.update(found or [])
    seen = [k for k, i in enumerate(order) if labels[i] in observed]
    after = len(labels) - 1 - seen[-1] if seen else len(labels)
    n = after if after > 0 else (periods or 8)
    return EndpointScoreCall(
        mode="forecast",
        horizon=Horizon(dim=hdim, periods=n),
        data=data,
        future_covariates=FutureCovariates(future_covariates),
        output=OutputSelector.model_validate(output) if output is not None else OutputSelector(),
    )


def _zeros(shape: list[int]) -> object:
    if not shape:
        return 0.0
    return [_zeros(shape[1:]) for _ in range(shape[0])]
