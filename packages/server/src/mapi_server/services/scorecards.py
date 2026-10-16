"""The promotion gate of a stored model version, derived from its scorecard (M3).

The runner measures the scorecard at fit (``scorecard.json``) and the finalizer stores it on
the model version; :func:`promotion_decision` feeds it, with the version's status and fit
mode, to :func:`mapi_spec.scorecard.promotion_gate`. Promoting a version to an endpoint
applies the decision (and any administrator override) with
:func:`mapi_spec.scorecard.authorize_promotion`.
"""

from __future__ import annotations

from collections.abc import Sequence

from mapi_spec.endpoints import EvaluationPolicy
from mapi_spec.scorecard import PromotionGateDecision, Scorecard, promotion_gate
from mapi_spec.spec import FitMode
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m

__all__ = ["fit_mode_of", "promotion_decision", "stored_scorecard"]


def stored_scorecard(mv: m.ModelVersion) -> Scorecard | None:
    """The model version's scorecard (``None`` for versions without one)."""
    return Scorecard.model_validate(mv.scorecard) if mv.scorecard else None


async def fit_mode_of(session: AsyncSession, mv: m.ModelVersion) -> str:
    """``model.fit.mode`` of the fit that produced ``mv`` (the spec default when unset)."""
    run = await session.get(m.Run, mv.run_id)
    fit = ((run.payload if run is not None else {}).get("model") or {}).get("fit") or {}
    mode = fit.get("mode") if isinstance(fit, dict) else None
    return str(mode) if isinstance(mode, str) else FitMode.EVALUATE_THEN_REFIT.value


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
    )
