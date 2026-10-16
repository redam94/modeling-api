"""Rendering plot recipes inside the worker (``mapi_plots`` recipes + sanitizer).

A plot that cannot be drawn never fails the run: it becomes a ``warning`` event. Every plot
that *is* kept has passed the JSON sanitizer, which the control plane runs again.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable
from typing import Any

import xarray as xr
from mapi_plots.recipes import render_plots

from mapi_runner.worker.context import WorkerContext

__all__ = ["render_for"]


def render_for(
    ctx: WorkerContext,
    refs: Iterable[str],
    trees: Iterable[xr.DataTree | Callable[[], xr.DataTree | None]],
) -> list[dict[str, Any]]:
    """Render ``refs`` against each tree in turn until every plot is drawn.

    A tree may be given lazily (a callable), so expensive inputs (an evaluation score of the
    holdout) are only computed when some plot still needs them.
    """
    wanted = [str(r) for r in refs]
    if not wanted:
        return []
    done: dict[str, dict[str, Any]] = {}
    reasons: dict[str, str] = {}
    for source in trees:
        todo = [r for r in wanted if r.split("@")[0] not in done]
        if not todo:
            break
        tree = source() if callable(source) else source
        if tree is None:
            continue
        with warnings.catch_warnings():  # e.g. all-NaN bands over a forecast's history
            warnings.simplefilter("ignore", RuntimeWarning)
            report = render_plots(tree, todo, registry=ctx.registry)
        for plot in report.to_json():
            done[str(plot["id"])] = plot
        for issue in report.issues:
            tail = str(issue.pointer or "").rsplit("/", 1)[-1]
            if tail.isdigit() and int(tail) < len(todo):
                reasons[todo[int(tail)]] = issue.message + (
                    f" ({issue.untrusted_text[:500]})" if issue.untrusted_text else ""
                )
    for ref in wanted:
        if ref.split("@")[0] not in done:
            ctx.warning(
                "PLOT_SKIPPED",
                f"plot {ref!r} could not be rendered",
                untrusted_text=reasons.get(ref, "no input had the data it needs")[:8000],
            )
    return [done[r.split("@")[0]] for r in wanted if r.split("@")[0] in done]
