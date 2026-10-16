"""The promotion gate of a stored model version, derived from its scorecard (M3).

The runner measures the scorecard at fit (``scorecard.json``) and the finalizer stores it on
the model version; :func:`promotion_decision` feeds it, with the version's status and fit
mode (and, for a guided spec, its template's checks, which must all have run), to
:func:`mapi_spec.scorecard.promotion_gate`. Promoting a version to an endpoint
applies the decision (and any administrator override) with
:func:`mapi_spec.scorecard.authorize_promotion`.
"""

from __future__ import annotations

from collections.abc import Sequence

from mapi_spec.endpoints import EvaluationPolicy
from mapi_spec.guided import GUIDED_TEMPLATES
from mapi_spec.scorecard import PromotionGateDecision, Scorecard, promotion_gate
from mapi_spec.spec import FitMode
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m

__all__ = ["fit_mode_of", "promotion_decision", "required_checks_of", "stored_scorecard"]


def stored_scorecard(mv: m.ModelVersion) -> Scorecard | None:
    """The model version's scorecard (``None`` for versions without one)."""
    return Scorecard.model_validate(mv.scorecard) if mv.scorecard else None


async def fit_mode_of(session: AsyncSession, mv: m.ModelVersion) -> str:
    """``model.fit.mode`` of the fit that produced ``mv`` (the spec default when unset)."""
    run = await session.get(m.Run, mv.run_id)
    fit = ((run.payload if run is not None else {}).get("model") or {}).get("fit") or {}
    mode = fit.get("mode") if isinstance(fit, dict) else None
    return str(mode) if isinstance(mode, str) else FitMode.EVALUATE_THEN_REFIT.value


async def required_checks_of(session: AsyncSession, mv: m.ModelVersion) -> list[str]:
    """The scorecard checks a guided spec's template (``metadata.labels.guided``) names: each
    one must have run for the version to pass the gate without an override."""
    run = await session.get(m.Run, mv.run_id)
    meta = (run.payload if run is not None else {}).get("metadata") or {}
    labels = meta.get("labels") if isinstance(meta, dict) else None
    guided = labels.get("guided") if isinstance(labels, dict) else None
    template = GUIDED_TEMPLATES.get(guided) if isinstance(guided, str) else None
    return list(template.scorecard_checks) if template is not None else []


async def promotion_decision(
    session: AsyncSession,
    mv: m.ModelVersion,
    *,
    policy: EvaluationPolicy | None = None,
    schema_breaks: Sequence[str] = (),
) -> PromotionGateDecision:
    """Whether ``mv`` may be promoted under the endpoint's ``policy`` (pure given the rows)."""
    policy = policy or EvaluationPolicy()
    return promotion_gate(
        stored_scorecard(mv),
        fit_mode=await fit_mode_of(session, mv),
        allow_train_only=policy.fit_mode is FitMode.TRAIN_ONLY,
        partial=mv.status == "partial",
        schema_breaks=schema_breaks,
        schema_change=policy.schema_change,
        required_checks=await required_checks_of(session, mv),
    )
