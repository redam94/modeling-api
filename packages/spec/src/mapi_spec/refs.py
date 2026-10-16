"""References used in specs: semver ranges, ``use:`` refs and resource URIs.

* :class:`Version` / :class:`VersionRange` — SemVer 2.0 with npm-style ranges
  (``^1``, ``~1.2``, ``>=1.2 <2``, ``1.x``, ``*``, ``||`` alternatives).
* :class:`UseRef` — ``core.resample@^1`` (step / model / plot ids with a range).
* :class:`DatasetRef` — ``dataset://project/name@version``.
* :class:`EndpointRef` — ``endpoint://project/alias``.
* :class:`ModelVersionRef` — ``model://project/name@v3``.
* :class:`SpecRef` — ``spec://project/name@r2`` (a spec revision; wrappers point at one).

All ref types validate from strings, serialise back to the exact same string and export a
JSON Schema ``pattern`` so editors can check them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import total_ordering
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler, PlainValidator, WithJsonSchema
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

__all__ = [
    "DatasetRef",
    "EndpointRef",
    "ModelTarget",
    "ModelVersionRef",
    "SpecRef",
    "UseRef",
    "Version",
    "VersionRange",
    "WorkflowRef",
    "parse_model_target",
    "resolve_version",
]

# ------------------------------------------------------------------------------------ SemVer

_NUM = r"0|[1-9]\d*"
_PRE_ID = r"[0-9A-Za-z-]+"
_SEMVER_RE = re.compile(
    rf"^({_NUM})\.({_NUM})\.({_NUM})"
    rf"(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?"
    rf"(?:\+({_PRE_ID}(?:\.{_PRE_ID})*))?$"
)


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    """A SemVer 2.0 version. Ordering follows SemVer precedence (build metadata ignored)."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def parse(cls, text: str) -> Version:
        m = _SEMVER_RE.match(text.strip().removeprefix("v"))
        if m is None:
            raise ValueError(f"not a semantic version: {text!r}")
        pre = tuple(m.group(4).split(".")) if m.group(4) else ()
        for ident in pre:
            if ident.isdigit() and len(ident) > 1 and ident.startswith("0"):
                raise ValueError(
                    f"numeric prerelease identifiers may not have leading zeros: {text!r}"
                )
        build = tuple(m.group(5).split(".")) if m.group(5) else ()
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre, build)

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def _key(self) -> tuple[Any, ...]:
        if not self.prerelease:
            return (*self.core, 1, ())
        ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.prerelease)
        return (*self.core, 0, ids)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __lt__(self, other: Version) -> bool:
        return self._key() < other._key()

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(self.prerelease)
        if self.build:
            s += "+" + ".".join(self.build)
        return s


_Op = Literal["<", "<=", ">", ">=", "="]


@dataclass(frozen=True, slots=True)
class _Comparator:
    op: _Op
    version: Version
    explicit_pre: bool = False  # the user wrote a prerelease on this comparator

    def test(self, v: Version) -> bool:
        match self.op:
            case "<":
                return v < self.version
            case "<=":
                return v <= self.version
            case ">":
                return v > self.version
            case ">=":
                return v >= self.version
            case "=":
                return v == self.version


_PARTIAL_RE = re.compile(
    rf"^v?(?P<maj>{_NUM}|[xX*])(?:\.(?P<min>{_NUM}|[xX*]))?(?:\.(?P<pat>{_NUM}|[xX*]))?"
    rf"(?:-(?P<pre>{_PRE_ID}(?:\.{_PRE_ID})*))?(?:\+{_PRE_ID}(?:\.{_PRE_ID})*)?$"
)
_COMPARATOR_RE = re.compile(r"^(\^|~|>=|<=|>|<|=)?\s*(\S+)$")


def _lower0(major: int, minor: int = 0, patch: int = 0) -> Version:
    """Smallest version with this core (``X.Y.Z-0``): excludes prereleases of the bound."""
    return Version(major, minor, patch, ("0",))


def _desugar(token: str) -> list[_Comparator]:
    m = _COMPARATOR_RE.match(token)
    if m is None:
        raise ValueError(f"invalid version comparator: {token!r}")
    op, body = m.group(1) or "", m.group(2)
    pm = _PARTIAL_RE.match(body)
    if pm is None:
        raise ValueError(f"invalid version in range: {body!r}")

    def num(g: str | None) -> int | None:
        return None if g is None or g in "xX*" else int(g)

    maj, mi, pa = num(pm.group("maj")), num(pm.group("min")), num(pm.group("pat"))
    if maj is None and (mi is not None or pa is not None):
        raise ValueError(f"invalid wildcard placement: {body!r}")
    if mi is None and pa is not None:
        raise ValueError(f"invalid wildcard placement: {body!r}")
    pre = tuple(pm.group("pre").split(".")) if pm.group("pre") else ()
    if pre and (mi is None or pa is None):
        raise ValueError(f"a prerelease needs a full version: {body!r}")
    explicit = bool(pre)

    if maj is None:  # '*' with any operator
        if op in ("<", ">"):
            return [_Comparator("<", Version(0, 0, 0, ("0",)))]  # matches nothing
        return []

    full = mi is not None and pa is not None
    lo = Version(maj, mi or 0, pa or 0, pre)

    def cmp(o: _Op, v: Version) -> _Comparator:
        return _Comparator(o, v, explicit)

    if op == "^":
        if maj > 0 or mi is None:
            hi = _lower0(maj + 1)
        elif mi > 0 or pa is None:
            hi = _lower0(0, mi + 1)
        else:
            hi = _lower0(0, 0, (pa or 0) + 1)
        return [cmp(">=", lo), cmp("<", hi)]
    if op == "~":
        hi = _lower0(maj + 1) if mi is None else _lower0(maj, mi + 1)
        return [cmp(">=", lo), cmp("<", hi)]
    if full:
        if op in ("", "="):
            return [cmp("=", lo)]
        return [cmp(op, lo)]  # type: ignore[arg-type]
    # partial version (X or X.Y) with an operator
    nxt = _lower0(maj + 1) if mi is None else _lower0(maj, mi + 1)
    match op:
        case "" | "=":
            return [cmp(">=", lo), cmp("<", nxt)]
        case ">=":
            return [cmp(">=", lo)]
        case ">":
            return [cmp(">=", nxt)]
        case "<":
            return [cmp("<", _lower0(*lo.core))]
        case "<=":
            return [cmp("<", nxt)]
    raise ValueError(f"invalid version comparator: {token!r}")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class VersionRange:
    """An npm-style SemVer range. ``str(range)`` returns the text it was parsed from."""

    text: str
    alternatives: tuple[tuple[_Comparator, ...], ...]

    @classmethod
    def parse(cls, text: str) -> VersionRange:
        raw = text.strip()
        if len(raw) > 256:
            raise ValueError("version range too long")
        if raw in ("", "*", "x", "X", "latest"):
            return cls(text, ((),))
        alts: list[tuple[_Comparator, ...]] = []
        for alt in raw.split("||"):
            alt = re.sub(r"(\^|~|>=|<=|>|<|=)\s+", r"\1", alt.strip())
            tokens = [t for t in re.split(r"[\s,]+", alt) if t]
            if not tokens:
                raise ValueError(f"empty alternative in version range: {text!r}")
            comps: list[_Comparator] = []
            for tok in tokens:
                comps.extend(_desugar(tok))
            alts.append(tuple(comps))
        return cls(text, tuple(alts))

    @staticmethod
    def _set_ok(comps: tuple[_Comparator, ...], v: Version) -> bool:
        if not all(c.test(v) for c in comps):
            return False
        if not v.is_prerelease:
            return True
        # npm rule: a prerelease only matches if some comparator explicitly names a
        # prerelease of the same major.minor.patch.
        return any(c.explicit_pre and c.version.core == v.core for c in comps)

    def satisfies(self, version: Version | str) -> bool:
        v = Version.parse(version) if isinstance(version, str) else version
        return any(self._set_ok(alt, v) for alt in self.alternatives)

    def max_satisfying(self, versions: Iterable[Version | str]) -> Version | None:
        best: Version | None = None
        for raw in versions:
            v = Version.parse(raw) if isinstance(raw, str) else raw
            if self.satisfies(v) and (best is None or v > best):
                best = v
        return best

    def __str__(self) -> str:
        return self.text

    @property
    def is_exact(self) -> bool:
        return len(self.alternatives) == 1 and [c.op for c in self.alternatives[0]] == ["="]


def resolve_version(
    range_: VersionRange | str, available: Iterable[Version | str]
) -> Version | None:
    """Highest version in ``available`` satisfying ``range_`` (``None`` if none does)."""
    r = VersionRange.parse(range_) if isinstance(range_, str) else range_
    return r.max_satisfying(available)


# ---------------------------------------------------------------------- string-ref base class


class _StrRef:
    """Mixin making a frozen dataclass behave as a pydantic-validated string."""

    __slots__ = ()

    json_pattern: ClassVar[str]
    json_description: ClassVar[str]
    json_examples: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def parse(cls, text: str) -> Self:  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def _validate(cls, value: Any) -> Self:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError(f"expected a string for {cls.__name__}")
        return cls.parse(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        validator = core_schema.no_info_plain_validator_function(cls._validate)
        return core_schema.json_or_python_schema(
            json_schema=validator,
            python_schema=validator,
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        out: dict[str, Any] = {
            "type": "string",
            "pattern": cls.json_pattern,
            "description": cls.json_description,
        }
        if cls.json_examples:
            out["examples"] = list(cls.json_examples)
        return out


_ID_BODY = r"(?:(?:t|vetted)/[a-z0-9][a-z0-9-]{0,62}/)?[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+"
_ID_RE = re.compile(rf"^{_ID_BODY}$")
_SLUG = r"[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?"


@dataclass(frozen=True, slots=True)
class UseRef(_StrRef):
    """``<namespace>.<name>@<semver range>``, e.g. ``core.resample@^1``.

    The range is optional (``core.ppc_fan`` means any version). It is resolved at submit
    to an exact version with :meth:`resolve`.
    """

    id: str
    range: VersionRange

    json_pattern: ClassVar[str] = rf"^{_ID_BODY}(@[^@]{{1,256}})?$"
    json_description: ClassVar[str] = "Registered id with an optional semver range"
    json_examples: ClassVar[tuple[str, ...]] = ("core.resample@^1", "mmm.bayesian@^1.3")

    @classmethod
    def parse(cls, text: str) -> UseRef:
        ident, sep, rng = text.strip().partition("@")
        if not _ID_RE.match(ident):
            raise ValueError(
                f"invalid id {ident!r}: expected 'namespace.name' (lowercase, dot-separated)"
            )
        if sep and not rng:
            raise ValueError("empty version range after '@'")
        return cls(ident, VersionRange.parse(rng if sep else "*"))

    @property
    def namespace(self) -> str:
        return self.id.rsplit(".", 1)[0]

    def resolve(self, available: Iterable[Version | str]) -> Version | None:
        return self.range.max_satisfying(available)

    def pinned(self, version: Version | str) -> UseRef:
        return UseRef(self.id, VersionRange.parse(str(version)))

    def __str__(self) -> str:
        rng = str(self.range).strip()
        return self.id if rng in ("", "*") else f"{self.id}@{rng}"


_DATASET_VERSION = r"latest|v\d{1,9}|\d{1,9}|sha256:[0-9a-f]{64}"
_DATASET_RE = re.compile(rf"^dataset://({_SLUG})/({_SLUG})(?:@({_DATASET_VERSION}))?$")


@dataclass(frozen=True, slots=True)
class DatasetRef(_StrRef):
    """``dataset://<project>/<name>[@latest|v3|3|sha256:<hex>]`` (no version = latest)."""

    project: str
    name: str
    version: str | None = None

    json_pattern: ClassVar[str] = _DATASET_RE.pattern
    json_description: ClassVar[str] = "Dataset reference dataset://project/name@version"
    json_examples: ClassVar[tuple[str, ...]] = ("dataset://growth/eu_media_daily@latest",)

    @classmethod
    def parse(cls, text: str) -> DatasetRef:
        m = _DATASET_RE.match(text.strip())
        if m is None:
            raise ValueError(
                "invalid dataset ref: expected dataset://<project>/<name>[@latest|vN|sha256:<hex>]"
            )
        return cls(m.group(1), m.group(2), m.group(3))

    @property
    def is_latest(self) -> bool:
        return self.version in (None, "latest")

    @property
    def is_pinned(self) -> bool:
        return self.version is not None and self.version.startswith("sha256:")

    @property
    def version_number(self) -> int | None:
        if self.version is None or self.version == "latest" or self.is_pinned:
            return None
        return int(self.version.removeprefix("v"))

    def pinned(self, sha256_hex: str) -> DatasetRef:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256_hex):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return DatasetRef(self.project, self.name, f"sha256:{sha256_hex}")

    def __str__(self) -> str:
        base = f"dataset://{self.project}/{self.name}"
        return base if self.version is None else f"{base}@{self.version}"


_ENDPOINT_RE = re.compile(rf"^endpoint://({_SLUG})/({_SLUG})$")


@dataclass(frozen=True, slots=True)
class EndpointRef(_StrRef):
    """``endpoint://<project>/<alias>`` — a stable alias for a promoted model version."""

    project: str
    alias: str

    json_pattern: ClassVar[str] = _ENDPOINT_RE.pattern
    json_description: ClassVar[str] = "Endpoint alias endpoint://project/alias"

    @classmethod
    def parse(cls, text: str) -> EndpointRef:
        m = _ENDPOINT_RE.match(text.strip())
        if m is None:
            raise ValueError("invalid endpoint ref: expected endpoint://<project>/<alias>")
        return cls(m.group(1), m.group(2))

    def __str__(self) -> str:
        return f"endpoint://{self.project}/{self.alias}"


_MODEL_RE = re.compile(rf"^model://({_SLUG})/({_SLUG})@(latest|v\d{{1,9}}|\d{{1,9}})$")


@dataclass(frozen=True, slots=True)
class ModelVersionRef(_StrRef):
    """``model://<project>/<name>@v3`` — a registered model version."""

    project: str
    name: str
    version: str

    json_pattern: ClassVar[str] = _MODEL_RE.pattern
    json_description: ClassVar[str] = "Model version model://project/name@vN"

    @classmethod
    def parse(cls, text: str) -> ModelVersionRef:
        m = _MODEL_RE.match(text.strip())
        if m is None:
            raise ValueError("invalid model ref: expected model://<project>/<name>@vN|latest")
        return cls(m.group(1), m.group(2), m.group(3))

    def __str__(self) -> str:
        return f"model://{self.project}/{self.name}@{self.version}"


_SPEC_RE = re.compile(rf"^spec://({_SLUG})/({_SLUG})@(r\d{{1,9}}|\d{{1,9}}|sha256:[0-9a-f]{{64}})$")


@dataclass(frozen=True, slots=True)
class SpecRef(_StrRef):
    """``spec://<project>/<name>@r2`` (or ``@sha256:<hex>``) — an immutable spec revision."""

    project: str
    name: str
    revision: str

    json_pattern: ClassVar[str] = _SPEC_RE.pattern
    json_description: ClassVar[str] = "Spec revision spec://project/name@rN"

    @classmethod
    def parse(cls, text: str) -> SpecRef:
        m = _SPEC_RE.match(text.strip())
        if m is None:
            raise ValueError("invalid spec ref: expected spec://<project>/<name>@rN|sha256:<hex>")
        return cls(m.group(1), m.group(2), m.group(3))

    def __str__(self) -> str:
        return f"spec://{self.project}/{self.name}@{self.revision}"


_WORKFLOW_RE = re.compile(rf"^workflow://({_SLUG})/({_SLUG})@(latest|r\d{{1,9}}|\d{{1,9}})$")


@dataclass(frozen=True, slots=True)
class WorkflowRef(_StrRef):
    """``workflow://<project>/<name>@r3`` (or ``@latest``) — a workflow revision (M3.5)."""

    project: str
    name: str
    revision: str

    json_pattern: ClassVar[str] = _WORKFLOW_RE.pattern
    json_description: ClassVar[str] = "Workflow revision workflow://project/name@rN|latest"
    json_examples: ClassVar[tuple[str, ...]] = ("workflow://growth/ts-readiness@r2",)

    @classmethod
    def parse(cls, text: str) -> WorkflowRef:
        m = _WORKFLOW_RE.match(text.strip())
        if m is None:
            raise ValueError("invalid workflow ref: expected workflow://<project>/<name>@rN|latest")
        return cls(m.group(1), m.group(2), m.group(3))

    @property
    def revision_number(self) -> int | None:
        return None if self.revision == "latest" else int(self.revision.removeprefix("r"))

    def __str__(self) -> str:
        return f"workflow://{self.project}/{self.name}@{self.revision}"


def parse_model_target(value: Any) -> EndpointRef | ModelVersionRef:
    """Parse the ``model:`` field of a ScoreRequest (endpoint alias or model version)."""
    if isinstance(value, EndpointRef | ModelVersionRef):
        return value
    if not isinstance(value, str):
        raise ValueError("model must be an endpoint:// or model:// reference")
    if value.startswith("endpoint://"):
        return EndpointRef.parse(value)
    if value.startswith("model://"):
        return ModelVersionRef.parse(value)
    raise ValueError("model must be an endpoint:// or model:// reference")


ModelTarget = Annotated[
    EndpointRef | ModelVersionRef,
    PlainValidator(parse_model_target),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": f"(?:{_ENDPOINT_RE.pattern})|(?:{_MODEL_RE.pattern})",
            "description": "endpoint://project/alias or model://project/name@vN",
        }
    ),
]
