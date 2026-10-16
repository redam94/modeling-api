"""BayesInsight ``model_def.json`` as a spec for the built-in geo MMM (Q8, M3).

:func:`import_bayesinsight` turns a BayesInsight project (``model_def.json`` plus the
``metadata.json`` of its MFF data) into a :class:`~mapi_spec.guided.GuidedDraft`:

* an ingest mapping with the ``bayesinsight`` preset that gathers the media variables into
  one ``spend`` variable along ``channel`` and joins the panel key columns (``row_ids``
  without ``Period``) into one ``geo`` dim;
* a ``ModelSpec`` for ``mmm.hierarchical_geo`` whose pipeline and settings carry every
  BayesInsight feature that has an equivalent, and an ``IMPORT_UNSUPPORTED`` *warning* for
  each one that has none (nothing is dropped silently).

======================================  ===========================================================
BayesInsight                            ``mmm.hierarchical_geo`` spec
======================================  ===========================================================
``exog`` variable (the KPI)             target ``y``, mean-scaled over time per geo
``media`` variables                     ``spend(geo, channel, time)``, mean-scaled, 12-lag tensor
``adstock: delayed`` / ``geometric``    ``adstock.kind`` (one kind for every channel)
``adstock_prior`` (Delayed)             ``alpha ~ Beta(mu, nu)`` and ``theta ~ Exponential(lam)``
                                        per channel - exactly BayesInsight's distributions
``media_transform: hill`` / ``linear``  ``saturation.kind: hill`` / ``none``
``media_transform_prior`` (Hill)        ``half ~ LogNormal`` exactly; ``slope ~ Gamma`` matching
                                        the mean and sd of BayesInsight's ``1 + Exponential``
``random_coeff_dims`` of media          ``pooling: partial`` (``fixed_ind_coeff_dims``: ``none``,
                                        neither: ``complete``)
``control`` variables                   controls (``Global Standardize`` → ``core.standardize``)
``season``                              ``core.fourier`` (``period``, ``n_fourier``)
``localtrend``                          ``core.trend`` (a linear trend control; warned)
======================================  ===========================================================

Coefficient and intercept priors are **not** carried over: BayesInsight states them in the
KPI's own units while the geo MMM works on the mean-scaled KPI, so the same numbers would
mean something else (state them as business priors instead). Likelihoods other than
``Normal``, ``Weibull`` adstock, S-shaped saturation, time transforms and non-linear
deterministic transforms have no equivalent and are reported.

Pure JSON → JSON (no numpy): the control plane runs it on the uploaded documents.
``target: mmm.bayesinsight`` keeps the original draft for the planned dedicated plugin
(:func:`mapi_spec.guided.draft_from_bayesinsight`).
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Final

from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from mapi_spec.guided import (
    BayesInsightImport,
    GuidedDraft,
    draft_from_bayesinsight,
    safe_name,
)
from mapi_spec.ingest import (
    MFF_KEY_COLUMNS,
    BayesInsightVariable,
    DimMapping,
    IngestMapping,
    IngestPreset,
    MffOptions,
    VarMapping,
    WideVarMapping,
    bayesinsight_var_ref,
)
from mapi_spec.loader import dump_yaml
from mapi_spec.schema import DimSpec, Role
from mapi_spec.spec import API_VERSION, ModelSpec

__all__ = [
    "BAYESINSIGHT_GEO_DIM",
    "BAYESINSIGHT_L_MAX",
    "BAYESINSIGHT_TARGET",
    "BAYESINSIGHT_TIME_DIM",
    "import_bayesinsight",
]

BAYESINSIGHT_TARGET: Final = "mmm.hierarchical_geo@^1"
"""The built-in plugin a BayesInsight model is imported for (the default target)."""
BAYESINSIGHT_TIME_DIM: Final = "date"
BAYESINSIGHT_GEO_DIM: Final = "geo"
BAYESINSIGHT_L_MAX: Final = 12
"""Lags of the spend tensor: BayesInsight's ``delayed_adstock`` default ``l_max``."""

_PLUGIN_DIMS = frozenset(MFF_KEY_COLUMNS)
_FIT = {"mode": "evaluate_then_refit", "tune": 500, "draws": 500, "chains": 4, "seed": 20261015}


def import_bayesinsight(req: BayesInsightImport, *, project: str) -> GuidedDraft:
    """The draft of a BayesInsight project for ``req.target`` (default
    ``mmm.hierarchical_geo``). Raises ``IMPORT_UNSUPPORTED`` when the model cannot run there
    at all (no single KPI, no media variable, variables split by key columns that are not
    row ids, names that clash)."""
    if req.target == "mmm.bayesinsight":
        return draft_from_bayesinsight(req, project=project)
    return _Adapter(req, project).draft()


def _unsupported(message: str, pointer: str) -> MapiError:
    return MapiError(message, code=ErrorCode.IMPORT_UNSUPPORTED, pointer=pointer)


class _Adapter:
    def __init__(self, req: BayesInsightImport, project: str) -> None:
        self.req = req
        self.project = project
        self.meta = req.metadata
        self.issues: list[Issue] = []

    # ------------------------------------------------------------------ helpers
    def warn(self, pointer: str, message: str, *, info: bool = False) -> None:
        self.issues.append(
            Issue(
                code=ErrorCode.IMPORT_UNSUPPORTED,
                severity=Severity.INFO if info else Severity.WARNING,
                pointer=pointer,
                message=message[:2000],
            )
        )

    @staticmethod
    def extra(v: BayesInsightVariable, key: str) -> Any:
        return (v.model_extra or {}).get(key)

    def ptr(self, i: int, *rest: str) -> str:
        return "/".join(["/model_def/variable_details", str(i), *rest])

    def variable(self, i: int, v: BayesInsightVariable) -> str:
        """The MFF ``VariableName`` behind an analytic name (single series only)."""
        try:
            ref = bayesinsight_var_ref(v.variable_name, self.meta)
        except ValueError as exc:
            raise _unsupported(str(exc), self.ptr(i, "variable_name")) from None
        if ref.sel:
            raise _unsupported(
                f"{v.variable_name!r} is one of several series of {ref.variable!r} (split by "
                f"{sorted(ref.sel)}); mmm.hierarchical_geo needs one series per variable - "
                "aggregate those key columns in the MFF first",
                self.ptr(i, "variable_name"),
            )
        return ref.variable

    def common_transforms(self, i: int, v: BayesInsightVariable) -> None:
        dt = self.extra(v, "deterministic_transform")
        form = dt.get("functional_form") if isinstance(dt, dict) else None
        if form not in (None, "linear"):
            self.warn(
                self.ptr(i, "deterministic_transform"),
                f"{v.variable_name}: the {form!r} transform has no built-in equivalent and was "
                "not applied (the variable enters linearly)",
            )
        if self.extra(v, "time_transform") is not None:
            self.warn(
                self.ptr(i, "time_transform"),
                f"{v.variable_name}: time transforms (moving averages) were not applied",
            )

    # ------------------------------------------------------------------ draft
    def draft(self) -> GuidedDraft:
        req, meta = self.req, self.meta
        kpi = req.model_def.kpi
        if kpi is None:
            raise _unsupported(
                "model_def.json needs exactly one 'exog' variable (the KPI)",
                "/model_def/variable_details",
            )
        keys = [c for c in meta.row_ids if c != "Period"]
        combine = {BAYESINSIGHT_GEO_DIM: keys or ["Geography"]}
        if len(keys) > 1:
            self.warn(
                "/metadata/row_ids",
                f"mmm.hierarchical_geo has one panel dim: {' and '.join(keys)} are joined into "
                f"{BAYESINSIGHT_GEO_DIM!r} (labels such as 'US|Watches'), so effects that "
                "BayesInsight pools per key column are pooled across the joined panel",
                info=True,
            )
        details = list(enumerate(req.model_def.variable_details))
        kpi_index = next(i for i, v in details if v.variable_type == "exog")
        kpi_var = self.variable(kpi_index, kpi)
        y_name = safe_name(kpi_var)
        self.kpi(kpi_index, kpi)

        media = [(i, v) for i, v in details if v.variable_type == "media"]
        if not media:
            raise _unsupported(
                "mmm.hierarchical_geo needs at least one 'media' variable",
                "/model_def/variable_details",
            )
        # channels in label order: per-channel prior lists follow the channel coordinate
        media.sort(key=lambda iv: self.variable(*iv))
        channels: dict[str, str] = {}
        for i, v in media:
            name = self.variable(i, v)
            if name in channels:
                raise _unsupported(f"media variable {name!r} is listed twice", self.ptr(i))
            channels[name] = name
            self.common_transforms(i, v)
        settings, priors = self.media_settings(media, list(channels))

        controls: dict[str, VarMapping] = {}
        standardize: list[str] = []
        for i, v in details:
            if v.variable_type != "control":
                continue
            column = self.variable(i, v)
            name = safe_name(column)
            if name in (y_name, "spend", "fourier", "trend") or name in controls:
                raise _unsupported(
                    f"control {v.variable_name!r} would be named {name!r}, which is taken",
                    self.ptr(i, "variable_name"),
                )
            controls[name] = VarMapping(column=column, role=Role.CONTROL)
            self.control(i, v)
            if v.normalization == "Global Standardize":
                standardize.append(name)

        pipeline: list[dict[str, Any]] = []
        if standardize:
            pipeline.append(
                {"id": "std_controls", "use": "core.standardize@^1",
                 "with": {"vars": standardize, "over": ["time", "geo"],
                          "unseen_levels": "pooled"}}
            )  # fmt: skip
        pipeline += [
            {"id": "scale_spend", "use": "core.mean_scale@^1",
             "with": {"vars": ["spend"], "over": ["time"], "unseen_levels": "pooled"}},
            {"id": "scale_y", "use": "core.mean_scale@^1",
             "with": {"vars": ["y"], "over": ["time"], "unseen_levels": "pooled"}},
            {"id": "lags", "use": "core.lag_tensor@^1",
             "with": {"vars": ["spend"], "along": "time", "within": ["geo"],
                      "l_max": BAYESINSIGHT_L_MAX, "pad": "first"}},
        ]  # fmt: skip
        seasons = [(i, v) for i, v in details if v.variable_type == "season"]
        for n, (i, v) in enumerate(seasons):
            pipeline.append(self.season(i, v, "fourier" if len(seasons) == 1 else f"fourier_{n}"))
        trends = [(i, v) for i, v in details if v.variable_type == "localtrend"]
        for n, (i, v) in enumerate(trends):
            self.warn(
                self.ptr(i),
                f"{v.variable_name}: BayesInsight's local spline trend is approximated by a "
                "linear trend control shared by all geos",
            )
            out = "trend" if len(trends) == 1 else f"trend_{n}"
            pipeline.append(
                {"id": f"trend_{n}", "use": "core.trend@^1",
                 "with": {"along": "time", "out": out}}
            )  # fmt: skip
        for i, v in details:
            if v.variable_type in ("season", "localtrend") and self.extra(v, "coeff_prior"):
                self.warn(
                    self.ptr(i, "coeff_prior"),
                    f"{v.variable_name}: its coefficient prior is not carried over (controls "
                    "share the gamma_control prior)",
                    info=True,
                )
        if priors:
            settings["priors"] = priors

        # the data's own weekday anchor wins over metadata.json's bare 'Weekly' ('W' is
        # W-SUN): the ingest infers W-MON for Monday periods
        mff = MffOptions(
            freq=None,
            time_dim=BAYESINSIGHT_TIME_DIM,
            combine=combine,
            roles={kpi_var: "target", **{c: "spend" for c in channels},
                   **{vm.column: "control" for vm in controls.values()}},
        )  # fmt: skip
        mapping = IngestMapping(
            format=req.format,
            preset=IngestPreset.BAYESINSIGHT,
            mff=mff,
            bayesinsight=meta,
            dims={
                BAYESINSIGHT_TIME_DIM: DimMapping(
                    column="Period", spec=DimSpec(kind="time", policy="extend")
                ),
                BAYESINSIGHT_GEO_DIM: DimMapping(
                    column=BAYESINSIGHT_GEO_DIM,
                    spec=DimSpec(
                        kind="categorical", policy="new_levels", new_level_strategy="pooled"
                    ),
                ),
            },
            vars={y_name: VarMapping(column=kpi_var, role=Role.TARGET), **controls},
            wide={"spend": WideVarMapping(dim="channel", columns=channels, role=Role.SPEND)},
        )
        dataset_ref = f"dataset://{self.project}/{req.dataset}@latest"
        doc: dict[str, Any] = {
            "apiVersion": API_VERSION,
            "kind": "ModelSpec",
            "metadata": {"name": req.name, "project": self.project,
                         "labels": {"family": "mmm", "imported": "bayesinsight"}},
            "data": {
                "dataset": dataset_ref,
                "bind": {"dims": {"geo": BAYESINSIGHT_GEO_DIM, "channel": "channel",
                                  "time": BAYESINSIGHT_TIME_DIM},
                         "vars": {"y": y_name, "spend": "spend"}},
                "split": {"kind": "time_holdout", "dim": "time", "last": req.holdout_periods},
            },
            "pipeline": pipeline,
            "model": {"use": BAYESINSIGHT_TARGET, "settings": settings, "fit": dict(_FIT)},
            "outputs": {"derived": ["contributions", "response_curves", "marginal_cost"],
                        "plots": ["core.ppc_fan", "core.convergence", "mmm.decomposition",
                                  "mmm.response_curves"],
                        "ui_draws": 300},
            "evaluate": {"metrics": ["mape", "coverage_90"]},
            "execution": {"resource_class": "M", "timeout": "45m"},
        }  # fmt: skip
        try:
            spec = ModelSpec.model_validate(doc)
        except ValueError as exc:  # pragma: no cover - the adapter builds valid documents
            raise MapiError(
                "the imported model does not make a valid spec",
                code=ErrorCode.IMPORT_UNSUPPORTED,
                untrusted_text=str(exc)[:4000],
            ) from None
        return GuidedDraft(
            template_id="bayesinsight_import",
            template_version="1.1.0",
            dataset_ref=dataset_ref,
            mapping=mapping,
            spec=spec.model_dump(mode="json", by_alias=True, exclude_defaults=True),
            yaml=dump_yaml(spec),
            issues=self.issues,
        )

    # ------------------------------------------------------------------ per type
    def kpi(self, i: int, v: BayesInsightVariable) -> None:
        likelihood = self.extra(v, "likelihood")
        kind = likelihood.get("type") if isinstance(likelihood, dict) else None
        if kind not in (None, "Normal"):
            self.warn(
                self.ptr(i, "likelihood"),
                f"the {kind} likelihood has no equivalent: mmm.hierarchical_geo fits a Normal "
                "likelihood to the mean-scaled KPI",
            )
        if self.extra(v, "intercept_prior") is not None:
            self.warn(
                self.ptr(i, "intercept_prior"),
                "the intercept prior is in KPI units and is not carried over (the geo MMM "
                "uses its default prior on the mean-scaled KPI)",
                info=True,
            )
        if v.normalization not in (None, "none"):
            self.warn(
                self.ptr(i, "normalization"),
                f"KPI normalization {v.normalization!r} is replaced by mean scaling per geo",
                info=True,
            )
        self.common_transforms(i, v)

    def control(self, i: int, v: BayesInsightVariable) -> None:
        self.common_transforms(i, v)
        if v.sign is not None:
            self.warn(
                self.ptr(i, "sign"),
                f"{v.variable_name}: sign constraints on controls have no equivalent "
                "(control coefficients are unconstrained)",
            )
        if v.random_coeff_dims or v.fixed_ind_coeff_dims:
            self.warn(
                self.ptr(i, "random_coeff_dims"),
                f"{v.variable_name}: controls have one coefficient shared by all geos in "
                "mmm.hierarchical_geo",
            )
        if self.extra(v, "coeff_prior") is not None:
            self.warn(
                self.ptr(i, "coeff_prior"),
                f"{v.variable_name}: its coefficient prior is in KPI units and is not carried "
                "over (controls share the gamma_control prior)",
                info=True,
            )

    def season(self, i: int, v: BayesInsightVariable, out: str) -> dict[str, Any]:
        default = 365.25 / 7 if self.meta.periodicity == "Weekly" else 365.25
        period = self.extra(v, "period")
        order = self.extra(v, "n_fourier")
        period_f = float(period) if isinstance(period, int | float) and period > 0 else default
        order_i = int(order) if isinstance(order, int) and 1 <= order <= 50 else 5
        if v.random_coeff_dims:
            self.warn(
                self.ptr(i, "random_coeff_dims"),
                f"{v.variable_name}: the seasonal pattern is shared by all geos",
            )
        return {
            "id": f"season_{out}",
            "use": "core.fourier@^1",
            "with": {"along": "time", "period": round(period_f, 6), "order": order_i, "out": out},
        }

    def media_settings(
        self, media: list[tuple[int, BayesInsightVariable]], channels: list[str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        adstocks = Counter(str(self.extra(v, "adstock") or "delayed") for _, v in media)
        shapes = Counter(str(self.extra(v, "media_transform") or "hill") for _, v in media)
        adstock = self.adstock_kind(media, adstocks)
        saturation = self.saturation_kind(media, shapes)
        settings: dict[str, Any] = {
            "pooling": self.pooling(media),
            "adstock": {"kind": adstock, "l_max": BAYESINSIGHT_L_MAX},
            "saturation": {"kind": saturation},
        }
        priors: dict[str, Any] = {}
        if adstock == "delayed":
            alpha_a, alpha_b, lam = [], [], []
            for _, v in media:
                p = self.extra(v, "adstock_prior")
                p = p if isinstance(p, dict) else {}
                mu = _num(p.get("retention_rate_mean"), 0.2)
                nu = _num(p.get("retention_rate_std"), 10.0)
                mu = min(max(mu, 1e-3), 1 - 1e-3)
                alpha_a.append(round(mu * nu, 9))
                alpha_b.append(round((1 - mu) * nu, 9))
                lam.append(_num(p.get("lag_max"), 3.0))
            priors["alpha"] = _channel_prior("Beta", channels, alpha=alpha_a, beta=alpha_b)
            priors["theta"] = _channel_prior("Exponential", channels, lam=lam)
        if saturation == "hill":
            half_mu, half_sigma, n_mu, n_sd = [], [], [], []
            approximated = False
            for _, v in media:
                p = self.extra(v, "media_transform_prior")
                p = p if isinstance(p, dict) else {}
                k_ave = _num(p.get("K_ave"), 0.85)
                k_std = _num(p.get("K_std"), 0.6)
                n_ave = _num(p.get("n_ave"), 1.5)
                n_std = _num(p.get("n_std"), 1.2)
                half_mu.append(round(math.log(k_ave), 9))
                half_sigma.append(round(math.log(k_std / k_ave + 1), 9))
                # BayesInsight: n = 1 + Exponential(1 / (n_ave - 1)), i.e. mean n_ave and sd
                # n_ave - 1; a Gamma with that mean and sd is the closest positive prior
                n_mu.append(n_ave)
                n_sd.append(max(n_ave - 1, 1e-3) if n_ave > 1 else n_std)
                approximated = True
            priors["half"] = _channel_prior("LogNormal", channels, mu=half_mu, sigma=half_sigma)
            priors["slope"] = _channel_prior("Gamma", channels, mu=n_mu, sigma=n_sd)
            if approximated:
                self.warn(
                    "/model_def/variable_details",
                    "Hill shapes: the half-saturation prior K is carried exactly (LogNormal); "
                    "the slope prior 1 + Exponential is approximated by a Gamma with the same "
                    "mean and sd. BayesInsight indexes spend to its non-zero median, the geo "
                    "MMM to its mean over time",
                    info=True,
                )
        coeff = [i for i, v in media if self.extra(v, "coeff_prior") is not None]
        if coeff:
            self.warn(
                self.ptr(coeff[0], "coeff_prior"),
                "media coefficient priors are in KPI units and are not carried over; state "
                "what you believe about each channel as business priors (ROI ranges) instead",
            )
        return settings, priors

    def adstock_kind(
        self, media: list[tuple[int, BayesInsightVariable]], kinds: Counter[str]
    ) -> str:
        mapped = {"delayed": "delayed", "geometric": "geometric"}
        for i, v in media:
            kind = str(self.extra(v, "adstock") or "delayed")
            if kind not in mapped:
                self.warn(
                    self.ptr(i, "adstock"),
                    f"{v.variable_name}: {kind!r} adstock has no equivalent; it is fitted with "
                    "the model's common adstock instead",
                )
        usable = Counter({k: n for k, n in kinds.items() if k in mapped})
        if len(usable) > 1:
            self.warn(
                "/model_def/variable_details",
                f"mmm.hierarchical_geo uses one adstock kind for every channel; "
                f"{usable.most_common(1)[0][0]!r} (the most common) is used",
            )
        return mapped[usable.most_common(1)[0][0]] if usable else "geometric"

    def saturation_kind(
        self, media: list[tuple[int, BayesInsightVariable]], kinds: Counter[str]
    ) -> str:
        mapped = {"hill": "hill", "linear": "none"}
        for i, v in media:
            kind = str(self.extra(v, "media_transform") or "hill")
            prior = self.extra(v, "media_transform_prior")
            if kind not in mapped or (isinstance(prior, dict) and prior.get("type") == "SShaped"):
                self.warn(
                    self.ptr(i, "media_transform"),
                    f"{v.variable_name}: the S-shaped curve has no equivalent; the model's "
                    "common saturation curve is used",
                )
        usable = Counter({k: n for k, n in kinds.items() if k in mapped})
        if len(usable) > 1:
            self.warn(
                "/model_def/variable_details",
                f"mmm.hierarchical_geo uses one saturation curve for every channel; "
                f"{usable.most_common(1)[0][0]!r} (the most common) is used",
            )
        return mapped[usable.most_common(1)[0][0]] if usable else "hill"

    def pooling(self, media: list[tuple[int, BayesInsightVariable]]) -> str:
        modes = set()
        for _, v in media:
            if v.random_coeff_dims:
                modes.add("partial")
            elif v.fixed_ind_coeff_dims:
                modes.add("none")
            else:
                modes.add("complete")
            unknown = set(v.random_coeff_dims or []) - _PLUGIN_DIMS
            if unknown:  # pragma: no cover - BayesInsight only knows the MFF key columns
                modes.add("partial")
        if len(modes) > 1:
            self.warn(
                "/model_def/variable_details",
                "channels pool their effects across geos differently; mmm.hierarchical_geo "
                "uses partial pooling for all of them",
            )
            return "partial"
        return modes.pop()


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    v = float(value)
    return v if math.isfinite(v) and v > 0 else default


def _channel_prior(dist: str, channels: list[str], **params: list[float]) -> dict[str, Any]:
    """A per-channel prior; one shared value when every channel has the same parameters."""
    shared = all(len(set(values)) == 1 for values in params.values())
    if shared or len(channels) == 1:
        return {"distribution": dist, **{k: v[0] for k, v in params.items()}}
    return {"distribution": dist, "dims": ["channel"], **params}
