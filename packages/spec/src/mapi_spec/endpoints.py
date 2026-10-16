"""Endpoints: stable aliases for promoted model versions (plan §2, §8.2, §11; Q13; M3).

An endpoint ``endpoint://<project>/<alias>`` points at one model version at a time.
Integrations (the Python and TypeScript clients, Google Sheets, Atlas, BI exports) call the
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

from typing import Final, Literal

from pydantic import AwareDatetime, Field, JsonValue, field_serializer, model_validator

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, Sha256, TenantId
from mapi_spec._dto import dto_examples as _examples
from mapi_spec._types import Duration, LabelKey, LabelValue, Name, Scalar, Slug
from mapi_spec.capabilities import OutputSelector, Representation, ScoreMode, default_summary
from mapi_spec.coords import CoordSummary
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
from mapi_spec.xrjson import XrJsonDataset

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
    "endpoint_ref",
    "example_score_call",
    "schema_breaks",
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
    the dim a forecast extends.
    """

    inputs_stage: str = "raw"
    input_schema: DataSchema
    schema_hash: Sha256
    score_modes: list[ScoreMode] = Field(default_factory=list)
    representation: Representation | None = None
    horizon_dim: Name | None = None
    future_required: list[str] = Field(default_factory=list)
    fit_coords: dict[str, CoordSummary] = Field(default_factory=dict)

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
    version was promoted before - but the rollback is audited with its reason."""

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


def _zeros(shape: list[int]) -> object:
    if not shape:
        return 0.0
    return [_zeros(shape[1:]) for _ in range(shape[0])]
