"""Parameter recovery through the full spec → pipeline → fit → derived path (plan §13).

Data are simulated from the model's own structure on the weekly EUR grid the E33
pipeline produces (see :mod:`mapi_plugin_mmm.examples`), so the unit-free parameters
(adstock decay, saturation rate, noise) and the channel contributions in EUR must come
back. A moderate fit (4 markets x 3 channels x 2 years, 2 x 400 draws) keeps it a slow
test rather than a nightly one.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from mapi_plugin_mmm.examples import e33_dataset, e33_raw_schema
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry
from mapi_spec import load_model_spec

pytestmark = [pytest.mark.slow, pytest.mark.integration]


def test_recovers_carryover_saturation_and_contributions(
    registry: Registry, tables: dict, spec_text: Callable[..., str]
) -> None:
    data, truth = e33_dataset(seed=11)  # 4 markets x 3 channels x 104 weeks
    text = spec_text(
        **{
            "  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}\n": "",
            "mode: evaluate_then_refit": "mode: train_only",
            "tune: 150": "tune: 400",
            "draws: 100": "draws: 400",
            "ui_draws: 50": "ui_draws: 200",
        }
    )
    bundle = fit_spec(
        load_model_spec(text), data, tables, registry, raw_schema=e33_raw_schema(data)
    )
    post = bundle.model.posterior
    channels = list(truth.channels)

    # short chains: hold r-hat to 1.05 (the platform warns above 1.01)
    rhat = [d for d in bundle.diagnostics if d.code == "RHAT" and (d.value or 0) > 1.05]
    assert not rhat, [(d.var, d.value) for d in rhat]

    alpha = post["alpha"].sel(channel=channels).mean(["chain", "draw"]).values
    np.testing.assert_allclose(alpha, truth.alpha, atol=0.1)
    lam = post["lam"].sel(channel=channels).mean(["chain", "draw"]).values
    np.testing.assert_allclose(lam, truth.lam, rtol=0.3)

    sigma = float(post["sigma"].mean())
    # model-space noise: the generator's sigma divided by each market's mean scaled revenue
    y_mean = (truth.weekly["revenue_eur"].mean("week") / truth.revenue_scale).values
    assert sigma == pytest.approx(truth.sigma / float(y_mean.mean()), rel=0.35)

    contrib = bundle.model.derived["contributions"]["channel_contribution"]
    assert contrib.attrs["units"] == "EUR"
    got = contrib.sum(["geo", "time"]).mean("sample").sel(channel=channels).values
    want = truth.channel_contribution_eur().sum("market").sel(channel=channels).values
    np.testing.assert_allclose(got, want, rtol=0.2)
    # the channel ranking by contribution is right
    assert list(np.argsort(got)) == list(np.argsort(want))

    # the response curve at the observed spend reproduces the true total per channel
    rc = bundle.model.derived["response_curves"]["contribution"]
    at_one = rc.sel(multiplier=1.0).sum("geo").mean("sample").sel(channel=channels).values
    np.testing.assert_allclose(at_one, want, rtol=0.2)
