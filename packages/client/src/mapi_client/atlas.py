"""An Atlas model backed by a modeling-api endpoint: :class:`RemoteModel`.

`Atlas <https://github.com/redam94/optimizer-framework>`_ optimises budgets against any
object with Atlas's model interface (``atlas.core.interfaces.ModelProtocol``:
``predict(x: xr.Dataset) -> xr.DataArray`` and ``contributions(x) -> xr.Dataset``, plus
``AbstractModel``'s ``model_type`` and ``required_dimensions``). :class:`RemoteModel` has that
interface *structurally*: Atlas is never imported here (it is AGPL; this client is
Apache-2.0), so ``pip install 'mapi-client[atlas]'`` brings only the xarray runtime and you
install Atlas yourself. :func:`register_with_atlas` additionally registers the class as a
virtual subclass of Atlas's ``AbstractModel`` for code that checks ``isinstance``.

Each candidate allocation is scored by the endpoint (``POST /v1/endpoints/{alias}/score``,
inline XrJSON, ``Prefer: wait``):

* **KPI out.** The output variable ``kpi`` (default: the endpoint's target) at the posterior
  ``mean`` (``output="mean"``) or as ``draws`` along ``sample`` (``output="draws"``), summed
  over every other dim (``reduce``), so ``predict`` returns a scalar per candidate.
* **Batching.** :meth:`RemoteModel.predict_batch` scores many candidates. With ``batch_dim``
  (a dim of the endpoint's input that accepts new coordinates and that the model scores
  independently - e.g. a ``scenario`` dim) they are stacked along it into one request of up
  to ``max_batch`` candidates; otherwise each candidate is a request.
* **Caching.** Results are cached by the exact payload (LRU, ``cache_size``), so optimisers
  that re-evaluate a point do not pay twice.
* **Rate limits.** ``RATE_LIMITED`` answers the client did not wait out are retried with
  exponential backoff (honouring ``Retry-After``); ``QUOTA_EXCEEDED`` is raised at once.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from mapi_spec.api import MAX_PREFER_WAIT_SECONDS, EndpointSchema
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DimPolicy, Role

from mapi_client._m3_common import result_dataset, score_call
from mapi_client.errors import retry_after_of
from mapi_client.transfer import require_xarray
from mapi_client.xrjson import encode

if TYPE_CHECKING:
    import xarray as xr

    from mapi_client._sync import Client

__all__ = ["CacheInfo", "RemoteModel", "register_with_atlas"]

MODEL_TYPE: Final = "mapi.endpoint"
MAX_DRAWS: Final = 300
"""Draws a score request returns at most on the default tier (plan Q2)."""
_BATCHABLE: Final = {
    "categorical": DimPolicy.NEW_LEVELS,
    "integer": DimPolicy.EXTEND,
    "index": DimPolicy.EXTEND,
}


@dataclass(frozen=True, slots=True)
class CacheInfo:
    """Cache and traffic counters of a :class:`RemoteModel`."""

    hits: int
    misses: int
    requests: int
    """Score requests sent (a batched request counts once)."""
    size: int


def register_with_atlas() -> bool:
    """Register :class:`RemoteModel` as a virtual subclass of Atlas's ``AbstractModel``
    (``isinstance(model, AbstractModel)`` then holds). Returns ``True``; raises
    ``ImportError`` when Atlas is not installed."""
    try:
        from atlas.core.interfaces import AbstractModel
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Atlas is not installed: install it from its repository "
            "(https://github.com/redam94/optimizer-framework)"
        ) from exc
    AbstractModel.register(RemoteModel)
    return True


class RemoteModel:
    """An Atlas-compatible model that scores an endpoint.

    Args:
        client: A configured :class:`~mapi_client.Client` (its project selects the endpoint).
        endpoint: The endpoint alias (``geo-mmm-prod``).
        kpi: The output variable to optimise (default: the endpoint's target variable, or
            the only output variable).
        output: ``"mean"`` (the posterior mean) or ``"draws"`` (``draws`` posterior draws
            along a ``sample`` dim, for risk-aware objectives).
        draws: Draws per candidate with ``output="draws"`` (at most 300).
        kind: ``"expected"`` (the mean response) or ``"predictive"`` (with noise).
        reduce: How ``predict`` folds the KPI's dims (except ``sample``): ``"sum"`` (total
            KPI), ``"mean"``, or ``"none"`` (the full array).
        base: A template payload: each candidate's variables replace the template's
            (supply the controls, holidays and other inputs an allocation lacks).
        payload_field: Send candidates as the call's ``data`` (default) or ``scenario``.
        call: Further :class:`~mapi_spec.endpoints.EndpointScoreCall` fields
            (``mode``, ``horizon``, ``future_covariates``, ...).
        spend_var: The spend variable :meth:`allocation` writes (default: the endpoint's
            only ``spend`` variable).
        channel_dim: The dim :meth:`allocation` puts budget keys on.
        batch_dim: Stack candidates along this dim in one request (see the module doc).
        max_batch: Candidates per batched request.
        cache_size: Payloads remembered (0 disables the cache).
        wait: ``Prefer: wait`` seconds per call; ``timeout`` bounds one candidate's score.
        rate_limit_retries: Extra tries after a ``RATE_LIMITED`` error.
        backoff: First backoff in seconds (doubled each retry, capped at ``max_backoff``).
    """

    def __init__(
        self,
        client: Client,
        endpoint: str,
        *,
        kpi: str | None = None,
        output: Literal["mean", "draws"] = "mean",
        draws: int = 100,
        kind: Literal["expected", "predictive"] = "expected",
        reduce: Literal["sum", "mean", "none"] = "sum",
        base: xr.Dataset | None = None,
        payload_field: Literal["data", "scenario"] = "data",
        call: Mapping[str, Any] | None = None,
        spend_var: str | None = None,
        channel_dim: str = "channel",
        batch_dim: str | None = None,
        max_batch: int = 64,
        cache_size: int = 1024,
        wait: int = MAX_PREFER_WAIT_SECONDS,
        timeout: float | None = 600.0,
        rate_limit_retries: int = 5,
        backoff: float = 1.0,
        max_backoff: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if output not in ("mean", "draws"):
            raise ValueError("output must be 'mean' or 'draws'")
        if reduce not in ("sum", "mean", "none"):
            raise ValueError("reduce must be 'sum', 'mean' or 'none'")
        if not 1 <= draws <= MAX_DRAWS:
            raise ValueError(f"draws must be between 1 and {MAX_DRAWS}")
        if max_batch < 1 or cache_size < 0 or rate_limit_retries < 0:
            raise ValueError("max_batch must be >= 1; cache_size and retries >= 0")
        for name in ("data", "scenario", "output"):
            if call and name in call:
                raise ValueError(f"call may not set {name!r}: RemoteModel sends it")
        self.client = client
        self.endpoint = endpoint
        self.kpi = kpi
        self.output = output
        self.draws = draws
        self.kind = kind
        self.reduce = reduce
        self.base = base
        self.payload_field = payload_field
        self.call = dict(call or {})
        self.spend_var = spend_var
        self.channel_dim = channel_dim
        self.batch_dim = batch_dim
        self.max_batch = max_batch
        self.cache_size = cache_size
        self.wait = wait
        self.timeout = timeout
        self.rate_limit_retries = rate_limit_retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self._sleep = sleep
        self._schema: EndpointSchema | None = None
        self._cache: OrderedDict[str, xr.Dataset] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._requests = 0

    def __repr__(self) -> str:
        return (
            f"RemoteModel(endpoint={self.endpoint!r}, kpi={self.kpi!r}, output={self.output!r},"
            f" batch_dim={self.batch_dim!r})"
        )

    # -------------------------------------------------------------- Atlas model interface
    @property
    def model_type(self) -> str:
        return MODEL_TYPE

    @property
    def required_dimensions(self) -> list[str]:
        """The dims of the endpoint's input schema (fetched once)."""
        return list(self.schema().snapshot.input_schema.dims)

    def predict(self, x: xr.Dataset) -> xr.DataArray:
        """The KPI for one allocation: a scalar (``output="mean"``) or a ``sample`` array
        (``output="draws"``), unless ``reduce="none"``."""
        return self.predict_batch([x])[0]

    def contributions(self, x: xr.Dataset) -> xr.Dataset:
        """Every output variable of the endpoint for ``x`` (at the mean, or as draws),
        unreduced - including the decomposition variables a model returns."""
        return self._scored([x])[0].copy()

    # ------------------------------------------------------------------------ batching
    def predict_batch(self, xs: Sequence[xr.Dataset]) -> list[xr.DataArray]:
        """The KPI for many allocations, batched along ``batch_dim`` when configured."""
        return [self._reduce(ds) for ds in self._scored(xs)]

    def predict_budget(self, budget: Mapping[str, float]) -> float:
        """The (mean) KPI of one budget per channel (see :meth:`allocation`)."""
        value = self.predict(self.allocation(budget))
        return float(value.mean())

    def allocation(self, budget: Mapping[str, float]) -> xr.Dataset:
        """An allocation dataset from a budget per channel.

        Without ``base`` the spend variable is a vector along ``channel_dim``. With a
        ``base`` holding the spend variable, each channel's flighting across the other dims
        (weeks, markets) is kept and rescaled to the channel's budget (spread evenly when
        the base spends nothing on it).
        """
        require_xarray()
        import numpy as np
        import xarray as xr

        var = self._spend_var()
        channels = [str(c) for c in budget]
        totals = xr.DataArray(
            np.asarray([float(budget[c]) for c in channels], dtype="float64"),
            dims=[self.channel_dim],
            coords={self.channel_dim: channels},
        )
        if self.base is None or var not in self.base:
            return xr.Dataset({var: totals})
        pattern = self.base[var]
        if self.channel_dim not in pattern.dims:
            raise ValueError(f"base[{var!r}] has no {self.channel_dim!r} dim")
        pattern = pattern.sel({self.channel_dim: channels})
        others = [d for d in pattern.dims if d != self.channel_dim]
        current = pattern.sum(others)
        even = xr.ones_like(pattern) / max(1, int(np.prod([pattern.sizes[d] for d in others])))
        share = (pattern / current.where(current > 0, 1.0)).where(current > 0, even)
        return xr.Dataset({var: (share * totals).transpose(*pattern.dims)})

    def cache_info(self) -> CacheInfo:
        return CacheInfo(self._hits, self._misses, self._requests, len(self._cache))

    def clear_cache(self) -> None:
        self._cache.clear()

    def schema(self) -> EndpointSchema:
        """The endpoint's input schema (fetched once and kept)."""
        if self._schema is None:
            self._schema = self._retrying(lambda: self.client.endpoints.schema(self.endpoint))
        return self._schema

    # ----------------------------------------------------------------------- internals
    def _spend_var(self) -> str:
        if self.spend_var is not None:
            return self.spend_var
        spend = [
            name
            for name, v in self.schema().snapshot.input_schema.vars.items()
            if v.role in (Role.SPEND, Role.MEDIA)
        ]
        if len(spend) != 1:
            raise ValueError(f"set spend_var: the endpoint has spend variables {spend}")
        return spend[0]

    def _payload(self, x: xr.Dataset) -> xr.Dataset:
        if self.base is None:
            return x
        merged = self.base.copy()
        merged.update(x)
        return merged

    def _output(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "summary": ["mean"]}
        if self.output == "draws":
            out["draws"] = self.draws
        return out

    def _key(self, payload: xr.Dataset) -> str:
        doc = {
            "payload": encode(payload).model_dump(mode="json", by_alias=True),
            "field": self.payload_field,
            "call": self.call,
            "output": self._output(),
        }
        text = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(text.encode()).hexdigest()

    def _remember(self, key: str, ds: xr.Dataset) -> None:
        if self.cache_size == 0:
            return
        self._cache[key] = ds
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _scored(self, xs: Sequence[xr.Dataset]) -> list[xr.Dataset]:
        """The selected result dataset (all variables) for each candidate, from the cache
        or the endpoint."""
        require_xarray()
        payloads = [self._payload(x) for x in xs]
        keys = [self._key(p) for p in payloads]
        out: dict[str, xr.Dataset] = {}
        todo: dict[str, xr.Dataset] = {}
        for key, payload in zip(keys, payloads, strict=True):
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                self._hits += 1
                out[key] = hit
            elif key not in todo:
                self._misses += 1
                todo[key] = payload
        if todo:
            items = list(todo.items())
            if self.batch_dim is None:
                for key, payload in items:
                    out[key] = self._score_one(payload)
                    self._remember(key, out[key])
            else:
                for start in range(0, len(items), self.max_batch):
                    chunk = items[start : start + self.max_batch]
                    for (key, _), ds in zip(
                        chunk, self._score_stacked([p for _, p in chunk]), strict=True
                    ):
                        out[key] = ds
                        self._remember(key, ds)
        return [out[k] for k in keys]

    def _score_one(self, payload: xr.Dataset) -> xr.Dataset:
        return self._select(self._score(payload))

    def _score(self, payload: xr.Dataset) -> xr.DataTree:
        body = score_call({**self.call, self.payload_field: payload, "output": self._output()}, {})
        key = str(uuid.uuid4())  # one key per logical request, reused by every retry

        def send() -> xr.DataTree:
            self._requests += 1
            result = self.client.endpoints.score(
                self.endpoint,
                body,
                wait=self.wait,
                timeout=self.timeout,
                idempotency_key=key,
                download=True,
            )
            if result.tree is None:
                raise MapiError(
                    f"score run {result.run.id} returned no result tree",
                    code=ErrorCode.INFRA_ERROR,
                )
            return result.tree

        return self._retrying(send)

    def _retrying[T](self, fn: Callable[[], T]) -> T:
        attempt = 0
        while True:
            try:
                return fn()
            except MapiError as exc:
                if exc.code is not ErrorCode.RATE_LIMITED or attempt >= self.rate_limit_retries:
                    raise
                delay = min(self.max_backoff, self.backoff * (2**attempt))
                after = retry_after_of(exc)
                if after is not None:
                    delay = max(delay, min(after, self.max_backoff))
                attempt += 1
                self._sleep(delay + random.uniform(0, min(1.0, 0.1 * delay)))

    def _select(self, tree: xr.DataTree) -> xr.Dataset:
        if self.output == "draws":
            return result_dataset(tree, "draws")
        summary = result_dataset(tree, "summary")
        if "stat" in summary.dims:
            summary = summary.sel(stat="mean", drop=True)
        return summary

    def _batch_labels(self, n: int) -> list[Any]:
        snap = self.schema().snapshot
        dim = self.batch_dim
        assert dim is not None
        spec = snap.input_schema.dims.get(dim)
        if spec is None:
            raise ValueError(
                f"the endpoint has no {dim!r} dim to batch along "
                f"(its dims: {sorted(snap.input_schema.dims)})"
            )
        allowed = _BATCHABLE.get(spec.kind)
        if allowed is None or spec.policy is not allowed:
            raise ValueError(
                f"the endpoint cannot batch along {dim!r} (a {spec.kind} dim with policy "
                f"{spec.policy.value}); batching needs a categorical dim with new_levels or "
                "an integer/index dim with extend"
            )
        if spec.kind == "categorical":
            return [f"candidate-{i}" for i in range(n)]
        fitted = snap.fit_coords.get(dim)
        start = 0
        if fitted is not None and fitted.end is not None:
            start = int(str(fitted.end)) + 1
        return list(range(start, start + n))

    def _score_stacked(self, payloads: Sequence[xr.Dataset]) -> list[xr.Dataset]:
        import xarray as xr

        dim = self.batch_dim
        assert dim is not None
        labels = self._batch_labels(len(payloads))
        parts = []
        for payload in payloads:
            if dim in payload.dims:
                if payload.sizes[dim] != 1:
                    raise ValueError(f"a batched candidate must have one {dim!r} coordinate")
                payload = payload.isel({dim: 0}, drop=True)
            parts.append(payload)
        stacked = xr.concat(parts, dim=dim, coords="minimal", compat="override")
        stacked = stacked.assign_coords({dim: labels})
        scored = self._select(self._score(stacked))
        if dim not in scored.dims:
            raise MapiError(
                f"the endpoint's output has no {dim!r} dim, so the batch cannot be split",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        return [scored.sel({dim: label}, drop=True) for label in labels]

    def _reduce(self, ds: xr.Dataset) -> xr.DataArray:
        name = self._kpi_name(ds)
        da = ds[name]
        if self.reduce == "none":
            return da.copy()
        dims = [d for d in da.dims if d != "sample"]
        return da.sum(dims) if self.reduce == "sum" else da.mean(dims)

    def _kpi_name(self, ds: xr.Dataset) -> str:
        names = [str(v) for v in ds.data_vars]
        if self.kpi is not None:
            if self.kpi not in names:
                raise ValueError(f"the endpoint returned no {self.kpi!r} (it returned {names})")
            return self.kpi
        if len(names) == 1:
            return names[0]
        targets = [
            n
            for n, v in self.schema().snapshot.input_schema.vars.items()
            if v.role is Role.TARGET and n in names
        ]
        if len(targets) == 1:
            return targets[0]
        raise ValueError(f"set kpi: the endpoint returned several variables {names}")
