"""Shared constrained types, the strict model base and small helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Annotated, Any, ClassVar, Self, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    JsonValue,
    PlainSerializer,
    StringConstraints,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

__all__ = [
    "Duration",
    "JsonValue",
    "LabelKey",
    "LabelValue",
    "MetricName",
    "Name",
    "Scalar",
    "Sha256Hex",
    "Slug",
    "SortedFrozenSet",
    "StepId",
    "StrictModel",
]


class StrictModel(BaseModel):
    """Base for every contract model: unknown fields are rejected, instances are immutable."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


NAME_RE = r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$"
"""Dim and variable names (dataset or canonical)."""

STEP_ID_RE = r"^[a-z_][a-z0-9_]{0,62}$"
SLUG_RE = r"^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$"
METRIC_RE = r"^[a-z][a-z0-9_]{0,63}$"

Name = Annotated[str, StringConstraints(pattern=NAME_RE)]
StepId = Annotated[str, StringConstraints(pattern=STEP_ID_RE)]
Slug = Annotated[str, StringConstraints(pattern=SLUG_RE)]
MetricName = Annotated[str, StringConstraints(pattern=METRIC_RE)]
LabelKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9_.\-/]{0,62})$")]
LabelValue = Annotated[str, StringConstraints(max_length=63, pattern=r"^[A-Za-z0-9_.\-]*$")]
Scalar = str | int | float | bool
SHA256_HEX_RE = r"^[0-9a-f]{64}$"
Sha256Hex = Annotated[str, StringConstraints(pattern=SHA256_HEX_RE)]
"""A bare lowercase hex sha256 digest (64 characters, no ``sha256:`` prefix)."""

RESERVED_STAGE = "raw"
"""Name of the stage right after binding, before any pipeline step."""

T = TypeVar("T")


def _sorted_list(values: frozenset[Any]) -> list[Any]:
    return sorted(values, key=lambda v: (str(type(v)), v))


SortedFrozenSet = Annotated[
    frozenset[T],
    PlainSerializer(_sorted_list, return_type=list[Any], when_used="always"),
]
"""A frozenset that always serialises as a sorted list (deterministic canonical JSON)."""


def unique(items: Iterable[str]) -> list[str]:
    """Return the duplicated entries of ``items`` (in first-seen order)."""
    seen: set[str] = set()
    dups: list[str] = []
    for it in items:
        if it in seen and it not in dups:
            dups.append(it)
        seen.add(it)
    return dups


# ---------------------------------------------------------------------------------- Duration

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class Duration:
    """A non-negative duration written as ``1h30m``, ``45m``, ``90s`` (or an int of seconds).

    Serialises to its canonical form (largest units first, zero parts omitted), so
    ``"90m"`` and ``"1h30m"`` normalise identically.
    """

    __slots__ = ("seconds",)
    max_seconds: ClassVar[int] = 7 * 24 * 3600

    def __init__(self, seconds: int) -> None:
        if seconds < 0 or seconds > self.max_seconds:
            raise ValueError(f"duration must be between 0s and {self.max_seconds}s")
        self.seconds = seconds

    @classmethod
    def parse(cls, value: str | int | Duration) -> Self:
        if isinstance(value, Duration):
            return cls(value.seconds)
        if isinstance(value, bool):
            raise ValueError("duration must be a string like '45m' or an int of seconds")
        if isinstance(value, int):
            return cls(value)
        m = _DURATION_RE.match(value.strip())
        if not value.strip() or m is None or not any(m.groups()):
            raise ValueError("duration must look like '1h30m', '45m' or '90s'")
        h, mi, s = (int(g) if g else 0 for g in m.groups())
        return cls(h * 3600 + mi * 60 + s)

    def __str__(self) -> str:
        if self.seconds == 0:
            return "0s"
        h, rem = divmod(self.seconds, 3600)
        m, s = divmod(rem, 60)
        return "".join(f"{v}{u}" for v, u in ((h, "h"), (m, "m"), (s, "s")) if v)

    def __repr__(self) -> str:
        return f"Duration({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Duration) and other.seconds == self.seconds

    def __hash__(self) -> int:
        return hash(self.seconds)

    def __lt__(self, other: Duration) -> bool:
        return self.seconds < other.seconds

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        validator = core_schema.no_info_plain_validator_function(cls.parse)
        return core_schema.json_or_python_schema(
            json_schema=validator,
            python_schema=validator,
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {
            "type": "string",
            "pattern": r"^(?=.)(\d+h)?(\d+m)?(\d+s)?$",
            "description": "Duration such as '45m', '1h30m' or '90s'.",
            "examples": ["45m", "1h30m"],
        }
