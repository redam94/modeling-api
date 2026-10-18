#!/usr/bin/env python3
"""Build-time preparation of a runner image (run once, as the last build step).

1. **Catalog export.** Loads every installed step, model and plot plugin through the
   ``mapi.*`` entry points (``mapi_sdk.default_registry()``) and writes the static catalog
   JSON the control plane validates specs against, plus its ``sha256:`` digest
   (``mapi_spec.run.catalog_digest``). Plan §7: "catalog JSON is generated at image build".
2. **Compile-cache warm-up.** When PyMC is installed, compiles the log-density and gradient of
   a small model with the C backend, so the common PyTensor ops are already in the baked
   ``base_compiledir`` when a run starts (plan §8.5: the compiledir and ``NUMBA_CACHE_DIR``
   are set explicitly and baked for built-ins).

Writes, under ``--out`` (default ``/opt/mapi``)::

    catalog.json      the Catalog (sorted keys)
    catalog.digest    sha256:<hex>
    warm.json         what was warmed, library versions and timings

Any failure exits non-zero so the image build fails: an image whose catalog cannot be
exported is unusable.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_VERSIONED = (
    "mapi-spec",
    "mapi-sdk",
    "mapi-runner",
    "mapi-steps",
    "mapi-plots",
    "mapi-pymc",
    "mapi-sklearn",
    "mapi-plugin-mmm",
    "mapi-plugin-ts",
    "mapi-plugin-tabular",
    "mapi-plugin-statsmodels",
    "mapi-plugin-science",
    "mapi-workflows",
    "statsmodels",
    "scipy",
    "pymc",
    "pytensor",
    "nutpie",
    "numba",
    "numpy",
    "xarray",
    "arviz",
    "scikit-learn",
    "xgboost",
    "mmm-framework",
)


def export_catalog(out: Path) -> dict[str, Any]:
    from mapi_sdk import default_registry
    from mapi_spec.run import catalog_digest

    catalog = default_registry().catalog()
    if not catalog.steps:
        raise RuntimeError("no steps registered: is mapi-steps installed in this image?")
    if not catalog.models:
        raise RuntimeError("no model plugins registered: the image would not be able to fit")
    payload = json.dumps(catalog.model_dump(mode="json"), sort_keys=True, indent=1)
    (out / "catalog.json").write_text(payload + "\n", encoding="utf-8")
    digest = catalog_digest(catalog)
    (out / "catalog.digest").write_text(digest + "\n", encoding="utf-8")
    return {
        "digest": digest,
        "steps": len(catalog.steps),
        "models": sorted(m.id for m in catalog.models),
        "plots": len(catalog.plots),
    }


def warm_pytensor() -> dict[str, Any]:
    """Compile a small hierarchical model's logp and dlogp with the C backend."""
    import numpy as np
    import pymc as pm
    import pytensor

    rng = np.random.default_rng(0)
    x = rng.normal(size=(40, 3))
    g = np.repeat(np.arange(4), 10)
    y = x @ np.array([0.5, -0.2, 0.1]) + rng.normal(scale=0.3, size=40)
    coords = {"obs": np.arange(40), "feat": ["a", "b", "c"], "grp": np.arange(4)}
    with pm.Model(coords=coords) as model:
        mu_a = pm.Normal("mu_a", 0.0, 1.0)
        sigma_a = pm.HalfNormal("sigma_a", 1.0)
        a = pm.Normal("a", mu_a, sigma_a, dims="grp")
        beta = pm.Normal("beta", 0.0, 1.0, dims="feat")
        lam = pm.Gamma("lam", alpha=3.0, beta=1.0)
        sat = pm.math.sigmoid(lam * pm.math.dot(x, beta))
        sigma = pm.HalfNormal("sigma", 1.0)
        mu = a[g] + sat + pm.math.log1p(pm.math.exp(-pm.math.abs(mu_a)))
        pm.Normal("y", mu, sigma, observed=y, dims="obs")
    started = time.perf_counter()
    point = model.initial_point()
    logp = model.compile_logp()
    dlogp = model.compile_dlogp()
    value = float(logp(point))
    grad = dlogp(point)
    if not np.isfinite(value) or not np.all(np.isfinite(grad)):
        raise RuntimeError("warm-up model produced a non-finite log density")
    pm.draw(model["y"], draws=2, random_seed=0)
    return {
        "compiledir": str(pytensor.config.compiledir),
        "cxx": bool(pytensor.config.cxx),
        "seconds": round(time.perf_counter() - started, 2),
    }


def versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in _VERSIONED:
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("/opt/mapi"))
    parser.add_argument("--image", default=os.environ.get("MAPI_IMAGE_NAME", "unknown"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"image": args.image, "python": sys.version.split()[0]}
    report["catalog"] = export_catalog(args.out)
    if importlib.util.find_spec("pymc") is not None:
        report["pytensor"] = warm_pytensor()
        if not report["pytensor"]["cxx"]:
            raise RuntimeError("PyTensor found no C++ compiler; install g++ in the runtime image")
    report["versions"] = versions()
    (args.out / "warm.json").write_text(json.dumps(report, sort_keys=True, indent=1) + "\n")
    print(json.dumps(report["catalog"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
