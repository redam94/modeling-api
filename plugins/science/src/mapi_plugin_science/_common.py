"""Helpers the ``sci.*`` model modules share: seeded generators, a quiet PyMC, attr cleaning
of bundle groups and evenly spaced posterior draws."""

from __future__ import annotations

import hashlib
import logging
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

import numpy as np
import pymc as pm
import xarray as xr

__all__ = ["clean_attrs", "draw_index", "quiet", "rng"]


def rng(seed: int, name: str) -> np.random.Generator:
    """A generator keyed by ``seed`` and a stream ``name`` (same pair, same numbers)."""
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


@contextmanager
def quiet() -> Iterator[None]:
    """Silence PyMC's log (below errors) and every warning inside the block."""
    logger = logging.getLogger("pymc")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)


def clean_attrs(ds: xr.Dataset, engine: str, keep: Iterable[str] = ()) -> xr.Dataset:
    """``ds`` with the variables' attrs dropped (except ``keep``) and the dataset attrs set
    to the inference library (PyMC, its version) and ``engine``."""
    kept = frozenset(keep)
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {k: a for k, a in out[v].attrs.items() if k in kept}
    out.attrs = {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": engine,
    }
    return out


def draw_index(post: xr.Dataset, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Every chain and ``n // chains`` evenly spaced draw positions (at least one)."""
    chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
    per = min(draws, max(1, n // chains))
    idx = np.unique(np.linspace(0, draws - 1, per).round().astype(np.int64))
    return np.arange(chains), idx
