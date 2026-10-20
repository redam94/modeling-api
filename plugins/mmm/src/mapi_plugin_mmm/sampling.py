"""Sampling an mmm-framework model with live progress, chains, step metrics and cancellation.

Shared by every MMM plugin of this package: ``BayesianMMM.fit`` and the extension
models' ``BaseExtendedMMM.fit`` take the same keyword names (``draws``, ``tune``,
``chains``, ``target_accept``, ``random_seed``, ``nuts_sampler``, ``method`` and extra
keywords forwarded to ``pm.sample``), so :func:`run_sampler` drives both:

* NUTS through nutpie: PyMC's nutpie progress hook reports progress, chains and step
  metrics; a cancel request interrupts the main thread once (nutpie's blocking ``sample``
  then stops and returns its partial trace, which is discarded);
* NUTS through PyMC's own sampler: a ``pm.sample`` callback reports the same and raises
  ``RunCancelled`` to stop it;
* SMC and the approximations (MAP, ADVI, Pathfinder, Laplace) run without live reports.

A failure inside mmm-framework is ``USER_CODE_ERROR`` (its text is untrusted), unless the
run was cancelled meanwhile (``RunCancelled``).
"""

from __future__ import annotations

import _thread
import contextlib
import threading
from collections.abc import Callable, Iterator
from typing import Any

from mapi_pymc.engines import (
    EngineHooks,
    nutpie_progress,
    pymc_draw_callback,
    pymc_nutpie_progress,
)
from mapi_sdk.errors import RunCancelled
from mapi_sdk.models import FitContext
from mapi_spec.errors import ErrorCode, MapiError

from mapi_plugin_mmm.framework import EnginePlan, quiet

__all__ = ["run_sampler"]


def run_sampler(
    fit: Callable[..., Any],
    plan: EnginePlan,
    ctx: FitContext,
    *,
    window: tuple[float, float] = (0.1, 0.9),
) -> Any:
    """``fit(draws=plan.draws, random_seed=plan.seed, method=plan.method, **kw)`` with live
    progress in ``window`` of the run's progress bar (see the module doc). Returns what
    ``fit`` returns; never a partial result of a cancelled run."""
    kwargs: dict[str, Any] = {"progressbar": False}
    live: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
    if plan.method == "nuts":
        kwargs["nuts_sampler"] = plan.nuts_sampler
        kwargs["tune"] = plan.tune
        kwargs["chains"] = plan.chains
        if plan.target_accept is not None:
            kwargs["target_accept"] = plan.target_accept
        hooks = EngineHooks.for_context(ctx, progress=_window(ctx, *window))
        if plan.nuts_sampler == "nutpie":
            live = _nutpie_live(hooks, ctx)
        elif plan.nuts_sampler == "pymc":
            kwargs["callback"] = pymc_draw_callback(hooks, plan)
    elif plan.method == "smc":
        kwargs["chains"] = plan.chains
    else:
        kwargs.pop("progressbar")
    try:
        with quiet(), live:
            result = fit(draws=plan.draws, random_seed=plan.seed, method=plan.method, **kwargs)
    except MapiError:
        raise
    except KeyboardInterrupt:
        # nutpie stops on the interrupt the cancel request raised (see _nutpie_live)
        if _cancelled(ctx):
            raise RunCancelled("run was cancelled while sampling") from None
        raise
    except Exception as exc:
        if _cancelled(ctx):
            raise RunCancelled("run was cancelled while sampling") from exc
        raise MapiError(
            "mmm-framework failed to fit the model",
            code=ErrorCode.USER_CODE_ERROR,
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        ) from exc
    # an aborted nutpie run returns its partial trace: never make a model from it
    ctx.check_cancelled()
    return result


def _window(ctx: FitContext, lo: float, hi: float) -> Callable[[float, str], None]:
    def report(fraction: float, message: str) -> None:
        ctx.progress(lo + (hi - lo) * fraction, message)

    return report


def _cancelled(ctx: FitContext) -> bool:
    return ctx.cancel is not None and ctx.cancel.cancelled


@contextlib.contextmanager
def _nutpie_live(hooks: EngineHooks, ctx: FitContext) -> Iterator[None]:
    """Live progress, chains and step metrics of PyMC's nutpie sampler; a cancel request
    interrupts the main thread once."""
    report = nutpie_progress(hooks)
    interrupted = threading.Event()

    def on_progress(progress: list[Any]) -> None:
        report(progress)
        if _cancelled(ctx) and not interrupted.is_set():
            interrupted.set()
            _thread.interrupt_main()

    with pymc_nutpie_progress(on_progress):
        yield
