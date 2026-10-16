"""M3 logic shared by the sync and async clients that does no I/O: request bodies from
friendly arguments, the keyed long-poll budget, result selection and plain downloads."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mapi_spec.api import (
    ArtifactLink,
    EndpointPromote,
    EndpointScoreCall,
    GdprExport,
    PlotRender,
    PlotRenderRequest,
    PlotSpec,
    PriorTranslationRequest,
    SavedPlotCreate,
    WebhookCreate,
    WebhookUpdate,
)
from mapi_spec.calibration import BusinessPriors, LiftTest
from mapi_spec.endpoints import EvaluationPolicy
from mapi_spec.errors import IntegrityFailure
from mapi_spec.resources import ResourceClass
from mapi_spec.sharing import ModelPlotRef
from mapi_spec.webhooks import WebhookEventType
from pydantic import BaseModel, JsonValue

from mapi_client.xrjson import as_inline

if TYPE_CHECKING:
    import xarray as xr

__all__ = [
    "EXPORT_DONE",
    "Deadline",
    "UrlSink",
    "as_model",
    "export_done",
    "promote_body",
    "render_body",
    "render_link",
    "result_dataset",
    "saved_plot_body",
    "score_call",
    "translation_body",
    "webhook_create_body",
    "webhook_update_body",
]

EXPORT_DONE: Final = frozenset({"ready", "failed", "expired"})


def as_model[M: BaseModel](model: type[M], value: M | Mapping[str, Any]) -> M:
    """``value`` as ``model`` (a mapping is validated)."""
    return value if isinstance(value, model) else model.model_validate(dict(value))


# ------------------------------------------------------------------------------- endpoints


def score_call(
    call: EndpointScoreCall | Mapping[str, Any] | None, fields: Mapping[str, Any]
) -> EndpointScoreCall:
    """An :class:`EndpointScoreCall` from a body (model or mapping) and keyword overrides.

    ``data``/``scenario`` may be ``xr.Dataset`` objects (encoded as inline XrJSON), XrJSON
    mappings or ``dataset://`` references. ``None`` overrides are ignored.
    """
    if isinstance(call, EndpointScoreCall):
        base: dict[str, Any] = call.model_dump(mode="json", by_alias=True, exclude_unset=True)
    else:
        base = dict(call or {})
    base.update({k: v for k, v in fields.items() if v is not None})
    for name in ("data", "scenario"):
        if name in base:
            base[name] = as_inline(base[name])
    return EndpointScoreCall.model_validate(base)


def promote_body(
    model: str | None,
    *,
    model_version_id: str | None,
    override: bool,
    reason: str | None,
    expected_revision: int | None,
    evaluation_policy: EvaluationPolicy | Mapping[str, Any] | None,
    description: str | None,
) -> EndpointPromote:
    return EndpointPromote(
        model=model,
        model_version_id=model_version_id,
        override=override,
        reason=reason,
        expected_revision=expected_revision,
        evaluation_policy=None
        if evaluation_policy is None
        else as_model(EvaluationPolicy, evaluation_policy),
        description=description,
    )


@dataclass(slots=True)
class Deadline:
    """The keyed long-poll budget: each call asks the server to wait ``wait`` seconds (less
    near ``timeout``); the same ``Idempotency-Key`` collects the result."""

    wait: int
    timeout: float | None
    started: float

    @classmethod
    def start(cls, wait: int, timeout: float | None) -> Deadline:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0")
        return cls(wait, timeout, time.monotonic())

    def next_wait(self) -> int:
        if self.timeout is None:
            return self.wait
        remaining = self.timeout - (time.monotonic() - self.started)
        return max(0, min(self.wait, int(remaining)))

    @property
    def expired(self) -> bool:
        return self.timeout is not None and time.monotonic() - self.started >= self.timeout


def result_dataset(tree: xr.DataTree, group: str = "summary") -> xr.Dataset:
    """One group of a score result tree as a dataset; the root attributes (representation,
    output space, issues, ...) are carried over."""
    path = group.strip("/")
    available = sorted(n.path.strip("/") for n in tree.subtree if n.path != "/")
    if not path or path not in available:
        raise ValueError(f"the score result has no group {group!r} (it has {available})")
    import xarray as xr

    node = tree[path]
    if not isinstance(node, xr.DataTree):  # pragma: no cover - a variable, not a group
        raise ValueError(f"{group!r} is not a group of the score result")
    ds = node.to_dataset(inherit=False)
    ds.attrs = {**{str(k): v for k, v in tree.attrs.items()}, **ds.attrs}
    return ds


# ------------------------------------------------------------------------------- guided


def translation_body(
    business_priors: BusinessPriors | Mapping[str, Any],
    *,
    spec_id: str | None,
    revision: int | None,
    document: Mapping[str, JsonValue] | None,
    dataset: str | None,
    lift_tests: Sequence[LiftTest | Mapping[str, Any]],
    draws: int,
    seed: int | None,
    resource_class: ResourceClass | str | None,
) -> PriorTranslationRequest:
    return PriorTranslationRequest(
        spec_id=spec_id,
        revision=revision,
        document=dict(document) if document is not None else None,
        dataset=dataset,
        business_priors=as_model(BusinessPriors, business_priors),
        lift_tests=[as_model(LiftTest, t) for t in lift_tests],
        draws=draws,
        seed=seed,
        resource_class=ResourceClass(resource_class) if resource_class else None,
    )


# ------------------------------------------------------------------------------- webhooks


def webhook_create_body(
    url: str,
    events: Sequence[WebhookEventType | str],
    description: str | None,
    active: bool,
) -> WebhookCreate:
    return WebhookCreate(
        url=url,
        events=[WebhookEventType(e) for e in events],
        description=description,
        active=active,
    )


def webhook_update_body(
    *,
    url: str | None,
    events: Sequence[WebhookEventType | str] | None,
    description: str | None,
    active: bool | None,
) -> WebhookUpdate:
    fields: dict[str, Any] = {}
    if url is not None:
        fields["url"] = url
    if events is not None:
        fields["events"] = [WebhookEventType(e) for e in events]
    if description is not None:
        fields["description"] = description
    if active is not None:
        fields["active"] = active
    return WebhookUpdate.model_validate(fields)


# --------------------------------------------------------------------------------- plots


def _plot_ref(
    model_id: str | None, version: int | None, plot_id: str | None
) -> ModelPlotRef | None:
    given = [x is not None for x in (model_id, version, plot_id)]
    if not any(given):
        return None
    if not all(given):
        raise ValueError("a model plot needs model_id, version and plot_id")
    return ModelPlotRef(model_id=str(model_id), version=int(str(version)), plot_id=str(plot_id))


def saved_plot_body(
    title: str,
    *,
    model_id: str | None,
    version: int | None,
    plot_id: str | None,
    spec: PlotSpec | Mapping[str, Any] | None,
    description: str | None,
) -> SavedPlotCreate:
    return SavedPlotCreate(
        title=title,
        description=description,
        source=_plot_ref(model_id, version, plot_id),
        spec=None if spec is None else as_model(PlotSpec, spec),
    )


def render_body(
    plot: PlotSpec | Mapping[str, Any] | None,
    *,
    saved_plot_id: str | None,
    model_id: str | None,
    version: int | None,
    plot_id: str | None,
    format: str,
    scale: float,
    width: int | None,
    height: int | None,
) -> PlotRenderRequest:
    return PlotRenderRequest.model_validate(
        {
            "plot": None if plot is None else as_model(PlotSpec, plot),
            "saved_plot_id": saved_plot_id,
            "source": _plot_ref(model_id, version, plot_id),
            "format": format,
            "scale": scale,
            "width": width,
            "height": height,
        }
    )


def render_link(render: PlotRender) -> ArtifactLink:
    """The rendered file of a finished render (the output in its format)."""
    if not render.outputs:
        raise ValueError(f"render {render.run.id} has no output (state {render.run.state.value})")
    for link in render.outputs:
        if link.name.endswith(f".{render.format}"):
            return link
    return render.outputs[0]


# ------------------------------------------------------------------------------- exports


def export_done(export: GdprExport) -> bool:
    return export.state in EXPORT_DONE


class UrlSink:
    """A download from a signed link without a checksum (GDPR exports): written to a temp
    file next to ``dest`` and renamed into place when complete; the size is checked when
    the server announced it."""

    def __init__(self, dest: Path, expected_size: int | None) -> None:
        self.dest = dest
        self.expected = expected_size
        self.size = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
        self._fh = os.fdopen(fd, "wb")
        self._tmp = Path(tmp)

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.expected is not None and self.size > self.expected:
            raise IntegrityFailure(
                f"{self.dest.name} is larger than the {self.expected} bytes announced"
            )
        self._fh.write(chunk)

    def finish(self) -> Path:
        self._fh.close()
        if self.expected is not None and self.size != self.expected:
            self.abort()
            raise IntegrityFailure(
                f"{self.dest.name} is {self.size} bytes, the export announced {self.expected}"
            )
        os.replace(self._tmp, self.dest)
        return self.dest

    def abort(self) -> None:
        with contextlib.suppress(OSError):
            self._fh.close()
        with contextlib.suppress(OSError):
            self._tmp.unlink()
