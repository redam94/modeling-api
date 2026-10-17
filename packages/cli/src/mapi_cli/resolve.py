"""Turning what people type into ids: models, datasets and workflows by reference or name.

``model://growth/geo-mmm@v3``, ``geo-mmm@v3``, ``geo-mmm@latest``, ``mdl_…@3`` or ``geo-mmm``
with ``--version 3`` all name one model version; datasets work the same way
(``dataset://growth/sales@v4``); workflows are named by id or by ``metadata.name``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from mapi_client import Client
from mapi_spec.api import Workflow
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.refs import DatasetRef, ModelVersionRef

__all__ = [
    "parse_assignments",
    "parse_json_value",
    "resolve_dataset",
    "resolve_model",
    "resolve_workflow",
]


def _not_found(what: str, name: str) -> MapiError:
    return MapiError(f"no {what} called {name!r} in this project", code=ErrorCode.NOT_FOUND)


def _version(text: str | int | None, latest: int | None, what: str) -> int:
    if text is None or text == "latest":
        if latest is None:
            raise MapiError(f"{what} has no version yet", code=ErrorCode.NOT_FOUND)
        return latest
    raw = str(text).removeprefix("v")
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"not a version: {text!r} (use vN, N or latest)")
    return int(raw)


def _split(ref: str, scheme: str) -> tuple[str, str | None]:
    if ref.startswith(f"{scheme}://"):
        parsed = (ModelVersionRef if scheme == "model" else DatasetRef).parse(
            ref if "@" in ref or scheme == "dataset" else f"{ref}@latest"
        )
        return parsed.name, parsed.version
    name, _, ver = ref.partition("@")
    return name, ver or None


def resolve_model(api: Client, ref: str, version: int | None = None) -> tuple[str, int]:
    """``(model_id, version)`` of a model version reference."""
    name, ver = _split(ref, "model")
    if version is not None:
        ver = str(version)
    if name.startswith("mdl_"):
        model = api.models.get(name)
    else:
        found = next((m for m in api.models.list(limit=200) if m.name == name), None)
        if found is None:
            raise _not_found("model", name)
        model = found
    return model.id, _version(ver, model.latest_version, f"model {model.name}")


def resolve_dataset(api: Client, ref: str, version: int | None = None) -> tuple[str, int]:
    """``(dataset_id, version)`` of a dataset reference (default: the latest version)."""
    name, ver = _split(ref, "dataset")
    if version is not None:
        ver = str(version)
    if name.startswith("ds_"):
        ds = api.datasets.get(name)
    else:
        found = api.datasets.find(name)
        if found is None:
            raise _not_found("dataset", name)
        ds = found
    return ds.id, _version(ver, ds.latest_version, f"dataset {ds.name}")


def resolve_workflow(api: Client, name_or_id: str) -> Workflow:
    """A workflow by id (``wf_…``) or by name."""
    if name_or_id.startswith("wf_"):
        return api.workflows.get(name_or_id)
    found = api.workflows.find(name_or_id)
    if found is None:
        raise _not_found("workflow", name_or_id)
    return found


def parse_json_value(text: str) -> Any:
    """A command-line value: JSON when it parses (``0.05``, ``[1, 4]``, ``true``), else text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_assignments(
    items: Sequence[str], *, what: str, json_values: bool = True
) -> dict[str, Any]:
    """``["alpha=0.05", "horizons=[1,4]"]`` -> ``{"alpha": 0.05, "horizons": [1, 4]}``."""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"{what} must look like name=value, not {item!r}")
        out[key] = parse_json_value(value) if json_values else value
    return out
