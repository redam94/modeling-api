"""XrJSON: ``xr.Dataset`` <-> the inline JSON payloads of endpoint score calls.

An endpoint call can carry its ``data`` and ``scenario`` inline as XrJSON
(:class:`mapi_spec.xrjson.XrJsonDataset`, the ``xr.Dataset.to_dict()`` shape, up to 5 MB) so
small scripts need no upload. :func:`encode` turns a dataset into that payload with the SDK's
wire format (``mapi_sdk.wire.to_xrjson``: datetimes as ISO strings, NaN as ``null``, the
element and attribute caps) and checks the envelope and size locally, so an oversized or
malformed payload fails before it is sent. :func:`decode` goes the other way (e.g. for
``EndpointSchema.example``) with the SDK's validating reader.

Both need the ``xarray`` extra (``pip install 'mapi-client[xarray]'``).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeGuard

from mapi_spec.xrjson import XrJsonDataset, check_xrjson_size

from mapi_client.transfer import require_xarray

if TYPE_CHECKING:
    import xarray as xr

__all__ = ["as_inline", "decode", "encode", "is_dataset"]

_VAR_KEYS = ("dims", "data", "attrs")


def is_dataset(obj: object) -> TypeGuard[xr.Dataset]:
    """``obj`` is an ``xr.Dataset`` (without importing xarray when it is not loaded)."""
    xr_mod = sys.modules.get("xarray")
    return xr_mod is not None and isinstance(obj, xr_mod.Dataset)


def _leaf_attrs(attrs: Mapping[str, Any]) -> dict[str, Any]:
    # XrJSON attributes are scalars; anything richer (lists, nested dicts) is metadata the
    # scorer does not read, so it is dropped rather than rejected
    return {
        str(k): v for k, v in attrs.items() if v is None or isinstance(v, str | int | float | bool)
    }


def encode(ds: xr.Dataset) -> XrJsonDataset:
    """An inline payload for ``ds`` (``PAYLOAD_TOO_LARGE`` over 5 MB; ``ValueError`` for an
    envelope the server would refuse, e.g. a dataset without data variables)."""
    require_xarray()
    from mapi_sdk.wire import to_xrjson

    raw = to_xrjson(ds)
    doc: dict[str, Any] = {
        "dims": raw.get("dims", {}),
        "attrs": _leaf_attrs(raw.get("attrs") or {}),
        "coords": {},
        "data_vars": {},
    }
    for section in ("coords", "data_vars"):
        for name, var in (raw.get(section) or {}).items():
            entry = {k: var[k] for k in _VAR_KEYS if k in var}
            entry["attrs"] = _leaf_attrs(entry.get("attrs") or {})
            doc[section][name] = entry
    check_xrjson_size(doc)
    return XrJsonDataset.model_validate(doc)


def decode(payload: XrJsonDataset | Mapping[str, Any]) -> xr.Dataset:
    """The ``xr.Dataset`` of an XrJSON payload (validated, size-capped)."""
    require_xarray()
    from mapi_sdk.wire import from_xrjson

    obj = (
        payload.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        if isinstance(payload, XrJsonDataset)
        else dict(payload)
    )
    return from_xrjson(obj)


def as_inline(value: object) -> object:
    """``value`` ready for ``EndpointScoreCall.data``/``scenario``: an ``xr.Dataset`` is
    encoded, an :class:`XrJsonDataset`, a mapping or a ``dataset://`` reference is passed on
    (the call model validates it)."""
    if is_dataset(value):
        return encode(value)
    return value
