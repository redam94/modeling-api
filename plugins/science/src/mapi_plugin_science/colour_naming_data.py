"""Data for ``sci.colour_naming`` (E77): raw schema, example specs, synthetic naming, the WCS.

* :data:`NAMING_SCHEMA` - ``response(speaker, chip)`` term labels of one language, with the
  CIELAB colour (``cie_l``, ``cie_a``, ``cie_b``) and the palette place (``wcs_row``,
  ``wcs_col``) of each chip;
* :data:`NAMING_SPEC_YAML` - a tiny spec (PyMC engine, few draws) for tests and the docs;
* :func:`naming_dataset` - naming simulated from the model's own forward process on a small
  WCS-like palette (greys plus a ring of hues per lightness row), truth :data:`NAMING_TRUTH`;
* :func:`from_challenges` - one language of the World Color Survey from the pymc-challenges
  files (``wcs_naming.npz``, ``wcs_terms.csv``, ``wcs_chips.csv``, ``wcs_languages.csv``),
  recoded like E77: the major terms (used by at least a third of the speakers) keep their
  WCS abbreviation and every other term becomes ``other``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.colour_naming import chip_features, naming_probabilities

__all__ = [
    "CHALLENGES_FILE",
    "CHALLENGES_FILES",
    "NAFAANRA_HELD_OUT",
    "NAFAANRA_SPEC_YAML",
    "NAMING_SCHEMA",
    "NAMING_SPEC_YAML",
    "NAMING_TRUTH",
    "ROWS",
    "from_challenges",
    "naming_dataset",
    "wcs_like_palette",
]

CHALLENGES_FILE = "wcs_naming.npz"
"""The naming file in the pymc-challenges ``data/`` directory (the CSVs sit next to it)."""
CHALLENGES_FILES = (CHALLENGES_FILE, "wcs_terms.csv", "wcs_chips.csv", "wcs_languages.csv")
"""Every file :func:`from_challenges` reads."""
ROWS = "ABCDEFGHIJ"
"""WCS palette rows, light to dark (``wcs_row`` 0-9)."""
NAFAANRA = 77
NAFAANRA_HELD_OUT = ("s03", "s04", "s07", "s13", "s18", "s19", "s22", "s25", "s27")
"""The 9 Nafaanra speakers E77 holds out (``default_rng(79).permutation(29)[20:]``)."""

NAMING_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "speaker": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
            "chip": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            "response": {
                "dims": ["speaker", "chip"],
                "dtype": "str",
                "role": "target",
                "missing": "allow",
            },
            "cie_l": {"dims": ["chip"], "dtype": "float64", "role": "covariate"},
            "cie_a": {"dims": ["chip"], "dtype": "float64", "role": "covariate"},
            "cie_b": {"dims": ["chip"], "dtype": "float64", "role": "covariate"},
            "wcs_row": {"dims": ["chip"], "dtype": "int64", "role": "covariate"},
            "wcs_col": {"dims": ["chip"], "dtype": "int64", "role": "covariate"},
        },
    }
)

_BIND = """
  bind:
    vars: {naming: response, lab_l: cie_l, lab_a: cie_a, lab_b: cie_b,
           grid_row: wcs_row, grid_col: wcs_col}"""

NAMING_SPEC_YAML = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: colour-naming, project: linguistics}}
data:
  dataset: dataset://linguistics/colour_naming@latest{_BIND}
  split: {{kind: leave_group_out, dim: speaker, n: 2, seed: 1}}
model:
  use: sci.colour_naming@^1
  settings: {{kind: hierarchical, new_speakers: 5, score_draws: 50}}
  fit: {{mode: evaluate_then_refit, seed: 77, tune: 150, draws: 100, chains: 2, engine: pymc}}
evaluate: {{metrics: [log_score, accuracy, accuracy_lift, brier, elpd_loo]}}
outputs: {{plots: [core.convergence, core.posterior_forest, sci.swatches, sci.field_map]}}
"""

NAFAANRA_SPEC_YAML = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: nafaanra-colour-naming, project: linguistics}}
data:
  dataset: dataset://linguistics/wcs_nafaanra@latest{_BIND}
  split: {{kind: leave_group_out, dim: speaker, groups: [{", ".join(NAFAANRA_HELD_OUT)}]}}
model:
  use: sci.colour_naming@^1
  settings: {{kind: hierarchical, features: cielab}}
  fit: {{mode: evaluate_then_refit, seed: 77, tune: 1000, draws: 1000, chains: 4,
        engine: nutpie}}
evaluate: {{metrics: [log_score, accuracy, accuracy_lift, brier, elpd_loo]}}
outputs: {{plots: [core.convergence, core.posterior_forest, sci.swatches, sci.field_map]}}
"""
"""E77 parts C and G: Nafaanra, the notebook's 9 held-out speakers scored as new speakers."""

NAMING_TRUTH: dict[str, Any] = {
    "terms": ("light", "dark", "warm"),
    # rows: the cielab features 1, L, a, b, L2, a2, b2, ab, La, Lb (made zero-sum per row)
    "W": [
        [0.0, 0.0, 0.0],
        [2.5, -2.5, 0.0],
        [-0.5, -0.5, 2.0],
        [0.0, 0.0, 0.6],
        [0.0, 0.0, -0.8],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
    ],
    "sd": 0.4,  # between-speaker sd of the intercept and the L, a, b weights
    "missing": 0.03,  # share of non-responses
}
"""Generating values of :func:`naming_dataset`."""


def wcs_like_palette(rows: int = 4, hues: int = 8, chroma: float = 45.0) -> pd.DataFrame:
    """A small palette shaped like the WCS one: ``rows`` lightness rows of ``hues`` chips
    around the hue circle (columns 1..hues), plus ``rows + 2`` greys in column 0 from white
    (row 0) to black (row ``rows + 1``). Columns ``chip, row, col, L, a, b``."""
    grey_l = np.linspace(96.0, 15.0, rows + 2)
    out = [(0, r, 0, grey_l[r], 0.0, 0.0) for r in range(rows + 2)]
    for r in range(1, rows + 1):
        for c in range(1, hues + 1):
            theta = 2 * np.pi * (c - 1) / hues
            out.append((0, r, c, grey_l[r], chroma * np.cos(theta), chroma * np.sin(theta)))
    df = pd.DataFrame(out, columns=["chip", "row", "col", "L", "a", "b"])
    df["chip"] = np.arange(1, len(df) + 1)
    return df


def naming_dataset(
    *,
    speakers: int = 12,
    rows: int = 4,
    hues: int = 8,
    seed: int = 0,
    truth: dict[str, Any] | None = None,
) -> xr.Dataset:
    """Naming ``response(speaker, chip)`` simulated from the hierarchical model.

    Each speaker's weights are ``W + sd z_s`` on the intercept and the linear ``L, a, b``
    features (``z_s`` zero-sum over terms), the probabilities a softmax of the standardised
    CIELAB quadratic features, and every chip is named once (a share ``missing`` of the
    answers is left empty).
    """
    p = {**NAMING_TRUTH, **(truth or {})}
    rng = np.random.default_rng(seed)
    pal = wcs_like_palette(rows, hues)
    lab = pal[["L", "a", "b"]].to_numpy()
    feats = chip_features(lab, pal["row"].to_numpy(), pal["col"].to_numpy(), "cielab")
    w = np.asarray(p["W"], dtype=float)
    w = w - w.mean(1, keepdims=True)
    terms = list(p["terms"])
    t = len(terms)
    z = rng.standard_normal((speakers, 4, t))
    z -= z.mean(-1, keepdims=True)
    prob = naming_probabilities(feats, w, np.full((4, t), float(p["sd"])), z)
    u = rng.random(prob.shape[:-1])[..., None]
    codes = np.minimum((prob.cumsum(-1) < u).sum(-1), t - 1)
    labels = np.asarray(terms, dtype=object)[codes]
    labels[rng.random(codes.shape) < float(p["missing"])] = ""
    return _dataset(
        labels.astype(str),
        [f"s{i + 1:02d}" for i in range(speakers)],
        pal,
        attrs={"source": "simulated from sci.colour_naming (NAMING_TRUTH)"},
    )


def _dataset(
    labels: np.ndarray, speakers: list[str], pal: pd.DataFrame, *, attrs: dict[str, str]
) -> xr.Dataset:
    chip = pal["chip"].to_numpy().astype(np.int64)
    ds = xr.Dataset(
        {
            "response": (("speaker", "chip"), labels),
            "cie_l": (("chip",), pal["L"].to_numpy(dtype=float)),
            "cie_a": (("chip",), pal["a"].to_numpy(dtype=float)),
            "cie_b": (("chip",), pal["b"].to_numpy(dtype=float)),
            "wcs_row": (("chip",), pal["row"].to_numpy().astype(np.int64)),
            "wcs_col": (("chip",), pal["col"].to_numpy().astype(np.int64)),
        },
        coords={"speaker": speakers, "chip": chip},
    )
    ds.attrs.update(attrs)
    return ds


# ------------------------------------------------------------------ the World Color Survey


def _need(df: pd.DataFrame, columns: list[str], name: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"{name} has no column(s) {missing}")


def from_challenges(
    path: str | Path, *, language: int = NAFAANRA, share: float = 1 / 3
) -> xr.Dataset:
    """One WCS language as ``response(speaker, chip)`` plus the chip table.

    ``path`` is ``wcs_naming.npz``; ``wcs_terms.csv``, ``wcs_chips.csv`` and
    ``wcs_languages.csv`` are read from the same directory. As in E77 (``lang_data``), a
    term is *major* if at least ``share`` of the language's speakers used it; major terms
    keep their WCS abbreviation, all others become ``other``, and non-responses become
    ``''``. Speakers are labelled ``s<WCS speaker number>``; ``wcs_row`` counts the palette
    rows A-J as 0-9. Language 77 is Nafaanra (4 major terms, 29 speakers).
    """
    path = Path(path)
    folder = path.parent
    with np.load(path) as npz:
        for key in ("naming", "lang", "speaker"):
            if key not in npz.files:
                raise ValueError(f"{path.name} has no array {key!r}")
        naming = np.asarray(npz["naming"])
        spk_lang = np.asarray(npz["lang"])
        spk_nr = np.asarray(npz["speaker"])
    terms = pd.read_csv(folder / "wcs_terms.csv")
    _need(terms, ["lang", "code", "abbrev", "term", "n_speakers"], "wcs_terms.csv")
    chips = pd.read_csv(folder / "wcs_chips.csv")
    _need(chips, ["chip", "row", "col", "grid", "L", "a", "b"], "wcs_chips.csv")
    langs = pd.read_csv(folder / "wcs_languages.csv")
    _need(langs, ["lang", "name"], "wcs_languages.csv")
    rows = np.flatnonzero(spk_lang == language)
    if rows.size == 0:
        raise ValueError(f"language {language} has no speakers in {path.name}")
    chips = chips.sort_values("chip").reset_index(drop=True)
    if naming.shape[1] != len(chips):
        raise ValueError(f"{path.name} has {naming.shape[1]} chips but wcs_chips.csv has {len(chips)}")
    y = naming[rows].astype(int)
    tl = terms[terms["lang"] == language].sort_values("code")
    major = (tl["n_speakers"] >= share * rows.size).to_numpy()
    names = np.full(int(tl["code"].max()) + 1, "other", dtype=object)
    names[tl["code"].to_numpy()[major]] = tl["abbrev"].to_numpy()[major]
    labels = np.where(y < 0, "", names[np.maximum(y, 0)]).astype(str)
    pal = pd.DataFrame(
        {
            "chip": chips["chip"].to_numpy(),
            "row": chips["row"].map(ROWS.index).to_numpy(),
            "col": chips["col"].to_numpy(),
            "L": chips["L"].to_numpy(),
            "a": chips["a"].to_numpy(),
            "b": chips["b"].to_numpy(),
        }
    )
    name = langs.set_index("lang")["name"].get(language, str(language))
    gloss = {str(a): str(t) for a, t in zip(tl["abbrev"], tl["term"], strict=True)}
    ds = _dataset(
        labels,
        [f"s{int(n):02d}" for n in spk_nr[rows]],
        pal,
        attrs={
            "source": "World Color Survey (Kay, Berlin, Maffi, Merrifield & Cook 2009) via "
            "pymc-challenges data/wcs_*.{npz,csv} (E77)",
            "language": str(name),
            "wcs_language": str(language),
            "glosses": json.dumps({k: v for k, v in gloss.items() if k in set(names.tolist())}),
        },
    )
    ds = ds.assign_coords(grid=("chip", chips["grid"].astype(str).to_numpy()))
    return ds
