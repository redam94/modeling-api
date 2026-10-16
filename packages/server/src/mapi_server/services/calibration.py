"""Static checks of calibration data against a dataset version's summary (M3, pure JSON).

Lift tests name a channel, optionally a geo, and a date window in the dataset's own labels.
The control plane checks them against the :class:`~mapi_spec.coords.DataSummary` that ingest
stored (categorical levels, the time range) so a typo is refused at submit, with a pointer,
instead of after a sandbox start. The dataset dims are found through the spec's binding of
the conventional canonical names ``channel``, ``geo`` and ``time``; a summary without the
levels (very many of them) or without a dim skips that part - the runner checks the data
itself before fitting.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from mapi_spec.calibration import LiftTest
from mapi_spec.coords import DataSummary, parse_time
from mapi_spec.errors import ErrorCode, Issue
from mapi_spec.spec import ModelSpec

__all__ = ["channel_dim", "check_lift_tests", "check_spec_calibration", "summary_levels"]


def _dim(spec: ModelSpec, canonical: str) -> str:
    return spec.data.bind.dims.get(canonical, canonical)


def channel_dim(spec: ModelSpec) -> str:
    """The dataset dim bound to the canonical ``channel``."""
    return _dim(spec, "channel")


def summary_levels(summary: DataSummary | None, dim: str) -> list[object] | None:
    """The categorical levels of ``dim`` in ``summary`` (``None`` when not listed)."""
    if summary is None or dim not in summary.coords:
        return None
    values = summary.coords[dim].values
    return list(values) if values is not None else None


def _day(value: object) -> date | None:
    try:
        return parse_time(value).date()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def check_lift_tests(
    tests: Sequence[LiftTest],
    spec: ModelSpec,
    summary: DataSummary | None,
    *,
    pointer: str = "/calibration/lift_tests",
) -> list[Issue]:
    """``SCHEMA_INVALID`` issues for lift tests whose channel, geo or window the dataset
    does not have."""
    if not tests or summary is None:
        return []
    channels = summary_levels(summary, channel_dim(spec))
    geos = summary_levels(summary, _dim(spec, "geo"))
    tcoord = summary.coords.get(_dim(spec, "time"))
    first = last = None
    if tcoord is not None and tcoord.kind == "time" and tcoord.start is not None:
        first = _day(tcoord.start)
        last = _day(tcoord.end) if tcoord.end is not None else None
    known_ch = None if channels is None else {str(c) for c in channels}
    known_geo = None if geos is None else {str(g) for g in geos}
    out: list[Issue] = []
    for i, test in enumerate(tests):
        here = f"{pointer}/{i}"
        if known_ch is not None and test.channel not in known_ch:
            out.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/channel",
                    message=f"the data has no channel {test.channel!r}",
                    suggested_action="Use the channel name exactly as it appears in the data.",
                )
            )
        if test.geo is not None and known_geo is not None and test.geo not in known_geo:
            out.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/geo",
                    message=f"the data has no geo {test.geo!r}",
                )
            )
        if first is not None and test.start < first:
            out.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/start",
                    message=f"the test starts on {test.start}, before the data ({first})",
                )
            )
        if last is not None and test.end > last:
            out.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/end",
                    message=f"the test ends on {test.end}, after the data ({last})",
                )
            )
    return out


def check_spec_calibration(spec: ModelSpec, summary: DataSummary | None) -> list[Issue]:
    """Issues of the spec's own ``calibration`` block against the dataset summary."""
    cal = spec.calibration
    if cal is None or not cal.lift_tests:
        return []
    return check_lift_tests(cal.lift_tests, spec, summary)
