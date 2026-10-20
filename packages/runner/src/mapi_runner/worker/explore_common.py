"""Helpers shared by the Data Explorer modules: the skip exceptions and JSON scalars."""

from __future__ import annotations

import datetime
import math
from typing import Any

import numpy as np
import pandas as pd
from mapi_spec.views import SkipReason

__all__ = [
    "InsufficientData",
    "NeedsOption",
    "NotApplicable",
    "SectionSkipped",
    "fv",
    "jsonable",
]


class SectionSkipped(Exception):
    """A section that cannot be shown for this data (the message is shown to the user).

    ``reason`` tells the web app whether to hide the tab (``not_applicable``) or to keep it
    and say why (``insufficient_data``, ``needs_option``)."""

    reason: SkipReason = "insufficient_data"

    def __init__(self, message: str, *, reason: SkipReason | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class NotApplicable(SectionSkipped):
    """The section does not apply to this kind of data (no time dim, no polygons, ...)."""

    reason: SkipReason = "not_applicable"


class InsufficientData(SectionSkipped):
    """The section applies, but the data is too short or too sparse."""

    reason: SkipReason = "insufficient_data"


class NeedsOption(SectionSkipped):
    """The section applies, but needs a request option (a KPI, a season length)."""

    reason: SkipReason = "needs_option"


def jsonable(v: Any) -> str | int | float | bool | None:
    """A JSON scalar for a coordinate label or value (dates as ISO text, NaN as ``None``)."""
    if isinstance(v, np.datetime64 | pd.Timestamp | datetime.datetime | datetime.date):
        ts = pd.Timestamp(v)
        if pd.isna(ts):
            return None
        text: str = ts.date().isoformat() if ts == ts.normalize() else ts.isoformat()
        return text
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, bool | int | str) or v is None:
        return v[:256] if isinstance(v, str) else v
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")[:256]
    return str(v)[:256]


def fv(x: Any) -> float | None:
    """A finite float, or ``None``."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
