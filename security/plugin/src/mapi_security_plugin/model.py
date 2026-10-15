"""``security.evil`` — a hostile model that runs the isolation probes during ``fit``."""

from __future__ import annotations

from typing import Any, ClassVar

import xarray as xr
from mapi_sdk.models import CustomModel, FitConfig, FitContext, FitOutput, Fitted, ScoreContext
from mapi_spec.capabilities import Capabilities, Representation
from pydantic import BaseModel

_TEMPLATE: dict[str, Any] = {
    "params": {"target": {"type": "string", "default": "y"}},
    "template": {
        "dims": {"t": {"kind": "time", "policy": "extend"}},
        "vars": {"{target}": {"dims": ["t"], "dtype": "float64", "role": "target"}},
    },
}


class EvilSettings(BaseModel):
    target: str = "y"


def _run_isolation_probes() -> dict[str, str]:
    """Run the isolation probes and return ``{probe_id: outcome}`` (safe, bounded)."""
    from mapi_security.outcomes import Category
    from mapi_security.probes import ALL_PROBES

    results: dict[str, str] = {}
    for probe in ALL_PROBES:
        if probe.category is not Category.ISOLATION:
            continue
        try:
            results[probe.probe_id] = probe.run().outcome.value
        except Exception as exc:  # pragma: no cover - a probe bug, not a finding
            results[probe.probe_id] = f"error:{type(exc).__name__}"
    return results


class EvilModel(CustomModel[EvilSettings]):
    """Attempts every sandbox escape the probes implement, then reports what it got away with.

    On an enforcing executor (docker-gvisor, Cloud Run microVM) ``learned["probe_outcomes"]``
    is all ``blocked``; the security suite asserts exactly that.
    """

    id: ClassVar[str] = "security.evil"
    version: ClassVar[str] = "0.0.0"
    Settings: ClassVar[type[BaseModel]] = EvilSettings
    capabilities: ClassVar[Capabilities] = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"predict"}),
    )
    schema_template: ClassVar[dict[str, Any]] = _TEMPLATE
    description: ClassVar[str | None] = "MALICIOUS test-only model — never install in production."

    def fit(self, ds: xr.Dataset, s: EvilSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        outcomes = _run_isolation_probes()
        ctx.info(f"probe outcomes: {outcomes}")
        return FitOutput(learned={"probe_outcomes": outcomes})

    def score(self, fitted: Fitted, ds: xr.Dataset, req: Any, ctx: ScoreContext) -> xr.DataTree:
        # Re-run the probes at score time too, so scoring is also exercised.
        outcomes = _run_isolation_probes()
        root = xr.Dataset({"blocked": len([v for v in outcomes.values() if v == "blocked"])})
        return xr.DataTree(dataset=root)
