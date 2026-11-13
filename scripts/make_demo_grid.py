#!/usr/bin/env python3
"""Build the static what-if grid of the Home and Catalog playgrounds (v0.18 D21):
``apps/web/src/features/onboarding/demo-grid.json``.

The grid is a real fit, not hand-made numbers. The script

1. makes the sample project's table (``geo_mmm``: 104 weeks of revenue, TV, paid search and
   social spend and the average price in 4 UK regions; synthetic data with a known truth,
   deterministic for a seed);
2. takes it down the Guided path the sample project uses (column profiles → role
   suggestions → ``build_draft`` → ingest with the draft's mapping → static validation);
3. fits the draft in process with the sample's fit profile (``SAMPLE_FIT``, chosen by the
   v0.18 WP0 probe) and builds ``ui/grids/response_curves.json`` as the runner does
   (``mapi_runner.worker.guided.response_grid_view``);
4. shrinks that grid for the web bundle: the national grid only (no per-geo arrays), every
   other multiplier from 0 to 2.5 plus 1 and the top, as many evenly spaced draws as fit the
   size budget (at most 200), and every number rounded to 4 significant digits.

The result is a valid ``mapi.response-grid/v1`` document (``ResponseGridView``), so the web
playground runs it through the same ``evaluateScenario`` as a real model's grid.

    uv run python scripts/make_demo_grid.py                       # fit, write demo-grid.json
    uv run python scripts/make_demo_grid.py --write-csv sample.csv --no-grid   # the table only
    uv run python scripts/make_demo_grid.py --grid-in grid.json   # shrink an existing grid

The fit needs the runner, the SDK and the MMM plugin (``uv sync --all-packages``); it takes
about a minute on a laptop CPU.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "apps/web/src/features/onboarding/demo-grid.json"

SAMPLE_SEED = 20261021
SAMPLE_WEEKS = 104
SAMPLE_START = "2024-01-07"
SAMPLE_MARKETS: dict[str, float] = {"north": 1.0, "south": 1.3, "midlands": 0.85, "scotland": 0.6}
"""Region → size of the region (multiplies the baseline and the spend)."""

SAMPLE_FIT: dict[str, Any] = {
    "mode": "train_only",
    "engine": "nutpie",
    "tune": 300,
    "draws": 300,
    "chains": 4,
}
"""The sample project's fit profile (WP0 (g); merged over the Guided draft's fit block)."""

MAX_DRAWS = 200
MAX_BYTES = 40 * 1024
SIGNIFICANT = 4
MULTIPLIER_STEP = 0.2


# ================================================================================ the table


def _hill(x: np.ndarray, half: float, shape: float) -> np.ndarray:
    xs: np.ndarray = np.power(np.maximum(x, 0.0), shape)
    out: np.ndarray = xs / (xs + half**shape)
    return out


def _adstock(x: np.ndarray, decay: float, l_max: int = 8) -> np.ndarray:
    w = decay ** np.arange(l_max)
    w /= w.sum()
    return np.convolve(x, w)[: len(x)]


def sample_table(seed: int = SAMPLE_SEED) -> pd.DataFrame:
    """The ``geo_mmm`` sample: one row per week and region (GBP).

    Truth: a level per region with a gentle trend and a yearly season; TV in flights
    (geometric carry-over 0.6, slow saturation), paid search following the season (carry-over
    0.2), social always on (carry-over 0.35); a price effect; 3 % noise. Media drive about a
    fifth of revenue and every channel returns more than it costs.
    """
    rng = np.random.default_rng(seed)
    weeks = pd.date_range(SAMPLE_START, periods=SAMPLE_WEEKS, freq="W-SUN")
    t = np.arange(SAMPLE_WEEKS, dtype=float)
    season = np.sin(2 * np.pi * (t - 8) / 52.0)
    flights = ((t // 4) % 3 != 2).astype(float)  # 8 weeks on air, 4 off
    rows: list[dict[str, Any]] = []
    for market, size in SAMPLE_MARKETS.items():
        tv = size * (4_000 + 16_000 * flights) * rng.lognormal(0.0, 0.25, SAMPLE_WEEKS)
        search = size * 7_500 * (1 + 0.35 * season) * rng.lognormal(0.0, 0.15, SAMPLE_WEEKS)
        social = size * 3_800 * rng.lognormal(0.0, 0.35, SAMPLE_WEEKS)
        price = 9.99 + 0.25 * rng.standard_normal(SAMPLE_WEEKS).cumsum() / np.sqrt(t + 1)
        price = np.round(np.clip(price, 9.2, 10.8), 2)
        base = size * (150_000 + 120 * t + 18_000 * season)
        media = (
            size * 46_000 * _hill(_adstock(tv / size, 0.6), 14_000.0, 1.4)
            + size * 21_000 * _hill(_adstock(search / size, 0.2), 6_000.0, 1.1)
            + size * 8_500 * _hill(_adstock(social / size, 0.35), 3_500.0, 1.2)
        )
        price_effect = -size * 9_000 * (price - 9.99)
        revenue = (base + media + price_effect) * (1 + 0.03 * rng.standard_normal(SAMPLE_WEEKS))
        for k, week in enumerate(weeks):
            rows.append(
                {
                    "week": week.strftime("%Y-%m-%d"),
                    "region": market,
                    "Revenue (GBP)": round(float(revenue[k]), 2),
                    "tv_spend": round(float(tv[k]), 2),
                    "search_spend": round(float(search[k]), 2),
                    "social_spend": round(float(social[k]), 2),
                    "avg_price": float(price[k]),
                }
            )
    return pd.DataFrame(rows)


def write_csv(df: pd.DataFrame, path: Path) -> str:
    """Write the table with ``\\n`` line ends; returns the file's sha256 (hex)."""
    text = df.to_csv(index=False, lineterminator="\n")
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ================================================================================ the fit


def fit_grid(csv: Path, fit: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit the Guided ``geo_mmm`` draft of ``csv`` with ``fit``; returns the response grid
    (JSON) and a short report (timings, convergence, scorecard verdicts)."""
    from mapi_runner.jobs import IngestJob
    from mapi_runner.worker.guided import response_grid_view
    from mapi_runner.worker.ingest import build_dataset
    from mapi_runner.worker.scorecard import fit_scorecard
    from mapi_runner.worker.tabular import IngestLimits, column_profiles, read_table
    from mapi_sdk.harness import fit_spec
    from mapi_sdk.registry import default_registry
    from mapi_spec.guided import GUIDED_TEMPLATES, GuidedAnswers, build_draft, suggest_roles
    from mapi_spec.spec import ModelSpec
    from mapi_spec.validate import validate_spec

    registry = default_registry()
    profiles = column_profiles(read_table(csv, "csv", IngestLimits()))
    template = GUIDED_TEMPLATES["geo_mmm"]
    suggested = suggest_roles(template, profiles)
    if suggested.issues:
        raise SystemExit(f"role detection left questions open: {suggested.issues}")
    answers = GuidedAnswers(
        template_id="geo_mmm",
        name="sample-geo-mmm",
        dataset="sample-geo-mmm",
        answers={q.question_id: q.answer for q in suggested.questions},
        columns=profiles,
    )
    draft = build_draft(template, answers, project="sample")
    ds, schema, _ = build_dataset(csv, IngestJob(mapping=draft.mapping))
    doc = ModelSpec.model_validate(draft.spec).model_dump(
        mode="json", by_alias=True, exclude_defaults=True
    )
    doc["model"]["fit"] = {**doc["model"].get("fit", {}), **fit}
    spec = ModelSpec.model_validate(doc)
    result = validate_spec(spec, registry.catalog(), schema)
    if not result.ok:
        raise SystemExit(f"the sample spec is invalid: {[i.message for i in result.errors]}")
    start = time.perf_counter()
    bundle = fit_spec(spec, ds, registry=registry, raw_schema=schema)
    fit_s = time.perf_counter() - start
    card = fit_scorecard(bundle, ds)
    grid = response_grid_view(bundle)
    if grid is None:
        raise SystemExit("the fit wrote no response grid")
    report = {
        "fit_s": round(fit_s, 1),
        "fit": doc["model"]["fit"],
        "scorecard": [
            (i.check or i.code, i.status, None if i.value is None else round(i.value, 4))
            for i in card.items
        ],
    }
    return grid.model_dump(mode="json", exclude_none=True), report


# ================================================================================ shrinking


def _sig(x: float, digits: int = SIGNIFICANT) -> float | int:
    """``x`` rounded to ``digits`` significant digits (integral values as ints)."""
    if x == 0 or not math.isfinite(x):
        return 0
    out = round(x, digits - 1 - math.floor(math.log10(abs(x))))
    return int(out) if float(out).is_integer() and abs(out) < 2**53 else out


def _round(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round(v) for v in value]
    if isinstance(value, float):
        return _sig(value)
    return value


def _pick_multipliers(multipliers: Sequence[float], step: float) -> list[int]:
    """Indices of the grid points kept: every ``step`` from 0, plus 1 and the top point."""
    keep = {
        i
        for i, m in enumerate(multipliers)
        if abs(m / step - round(m / step)) < 1e-6 or abs(m - 1.0) < 1e-9
    }
    keep.add(len(multipliers) - 1)
    return sorted(keep)


def _thin(grid: dict[str, Any], draws: int, cols: list[int]) -> dict[str, Any]:
    total = int(grid["draws"])
    rows = sorted({round(i * (total - 1) / max(1, draws - 1)) for i in range(draws)})
    out: dict[str, Any] = {
        k: v for k, v in grid.items() if k not in ("contribution_by_geo", "spend_by_geo", "geos")
    }
    out["multipliers"] = [grid["multipliers"][j] for j in cols]
    out["draws"] = len(rows)
    out["contribution"] = [
        [[per_c[j] for j in cols] for per_c in grid["contribution"][r]] for r in rows
    ]
    out["spend"] = [[row[j] for j in cols] for row in grid["spend"]]
    if grid.get("baseline") is not None:
        out["baseline"] = [grid["baseline"][r] for r in rows]
    rounded: dict[str, Any] = _round(out)
    return rounded


def _dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n"


def demo_grid(
    grid: dict[str, Any], *, max_draws: int = MAX_DRAWS, max_bytes: int = MAX_BYTES
) -> dict[str, Any]:
    """The web-sized grid: national, fewer multipliers, the most draws that fit ``max_bytes``."""
    from mapi_spec.views import ResponseGridView

    cols = _pick_multipliers(grid["multipliers"], MULTIPLIER_STEP)
    lo, hi = 1, min(max_draws, int(grid["draws"]))
    if len(_dumps(_thin(grid, lo, cols))) > max_bytes:
        raise SystemExit("even one draw does not fit the size budget")
    while lo < hi:  # the largest draw count whose document fits
        mid = (lo + hi + 1) // 2
        if len(_dumps(_thin(grid, mid, cols))) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    out = _thin(grid, lo, cols)
    ResponseGridView.model_validate(out)  # the playground reads it as a real grid
    return out


# ================================================================================ CLI


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="the demo grid (JSON)")
    p.add_argument("--seed", type=int, default=SAMPLE_SEED, help="seed of the sample table")
    p.add_argument("--csv", type=Path, help="fit this table instead of the generated sample")
    p.add_argument("--write-csv", type=Path, help="also write the generated sample table here")
    p.add_argument("--grid-in", type=Path, help="shrink this response grid (skips the fit)")
    p.add_argument("--grid-out", type=Path, help="also write the full response grid here")
    p.add_argument("--fit", type=json.loads, default=None, help="fit block (JSON) to merge")
    p.add_argument("--no-grid", action="store_true", help="only write the table (--write-csv)")
    p.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    args = p.parse_args(argv)

    table = sample_table(args.seed)
    if args.write_csv:
        digest = write_csv(table, args.write_csv)
        print(f"wrote {args.write_csv} ({len(table)} rows, sha256 {digest})")
    if args.no_grid:
        return 0

    if args.grid_in:
        grid = json.loads(args.grid_in.read_text(encoding="utf-8"))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            csv = args.csv or Path(tmp) / "sample-geo-mmm.csv"
            if not args.csv:
                write_csv(table, csv)
            grid, report = fit_grid(csv, {**SAMPLE_FIT, **(args.fit or {})})
        print(json.dumps(report))
        if args.grid_out:
            args.grid_out.write_text(json.dumps(grid), encoding="utf-8")

    out = demo_grid(grid, max_bytes=args.max_bytes)
    text = _dumps(out)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(
        f"wrote {args.out}: {out['draws']} draws x {len(out['channels'])} channels x "
        f"{len(out['multipliers'])} multipliers, {len(text.encode()):,} bytes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
