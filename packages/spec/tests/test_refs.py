from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_spec.refs import (
    DatasetRef,
    EndpointRef,
    ModelTarget,
    ModelVersionRef,
    SpecRef,
    UseRef,
    Version,
    VersionRange,
    resolve_version,
)
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit

# ------------------------------------------------------------------------------ Version


def test_version_parse_and_str() -> None:
    v = Version.parse("1.2.3-rc.1+build.5")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)
    assert v.prerelease == ("rc", "1")
    assert v.build == ("build", "5")
    assert str(v) == "1.2.3-rc.1+build.5"
    assert Version.parse("v2.0.0") == Version(2, 0, 0)


@pytest.mark.parametrize("bad", ["1", "1.2", "01.2.3", "1.2.3-01", "1.2.3-", "a.b.c", ""])
def test_version_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        Version.parse(bad)


def test_semver_precedence() -> None:
    order = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    parsed = [Version.parse(s) for s in order]
    assert sorted(reversed(parsed)) == parsed
    assert Version.parse("1.0.0+a") == Version.parse("1.0.0+b")
    assert hash(Version.parse("1.0.0+a")) == hash(Version.parse("1.0.0"))


versions = st.builds(
    Version,
    st.integers(0, 20),
    st.integers(0, 20),
    st.integers(0, 20),
    st.one_of(
        st.just(()),
        st.lists(
            st.one_of(
                st.from_regex(r"[a-z][a-z0-9]{0,4}", fullmatch=True),
                st.integers(0, 50).map(str),
            ),
            min_size=1,
            max_size=3,
        ).map(tuple),
    ),
)


@given(versions)
def test_version_roundtrip(v: Version) -> None:
    assert Version.parse(str(v)) == v


@given(versions, versions)
def test_version_total_order(a: Version, b: Version) -> None:
    assert (a < b) + (b < a) + (a == b) == 1


# -------------------------------------------------------------------------------- ranges

AVAILABLE = ["0.9.0", "1.0.0", "1.2.0", "1.3.0", "1.3.5", "1.4.0-beta.1", "2.0.0", "2.1.0"]


@pytest.mark.parametrize(
    ("rng", "expected"),
    [
        ("^1", "1.3.5"),
        ("^1.2", "1.3.5"),
        ("^1.3.1", "1.3.5"),
        ("~1.2", "1.2.0"),
        ("~1", "1.3.5"),
        ("1.x", "1.3.5"),
        ("1.2.*", "1.2.0"),
        ("1", "1.3.5"),
        ("1.3", "1.3.5"),
        ("=1.2.0", "1.2.0"),
        ("1.2.0", "1.2.0"),
        (">=1.3 <2", "1.3.5"),
        (">=1.3, <2", "1.3.5"),
        (">1.3", "2.1.0"),
        (">1.3.0 <1.4", "1.3.5"),
        ("<=1.2", "1.2.0"),
        ("<1", "0.9.0"),
        ("^0.9", "0.9.0"),
        ("*", "2.1.0"),
        ("latest", "2.1.0"),
        ("^3 || ^1", "1.3.5"),
        ("^1.4.0-beta.0", "1.4.0-beta.1"),
        ("^5", None),
        (">=2.2", None),
    ],
)
def test_range_resolution(rng: str, expected: str | None) -> None:
    got = resolve_version(rng, AVAILABLE)
    assert (str(got) if got else None) == expected


def test_prerelease_excluded_unless_named() -> None:
    assert not VersionRange.parse("^1").satisfies("1.4.0-beta.1")
    assert not VersionRange.parse(">=1.0.0").satisfies("2.0.0-rc.1")
    assert VersionRange.parse(">=1.4.0-beta.0 <1.5").satisfies("1.4.0-beta.1")
    # an explicit prerelease on another core version does not let this one in
    assert not VersionRange.parse(">=1.3.0-rc.1").satisfies("1.4.0-beta.1")


def test_caret_zero_majors() -> None:
    r = VersionRange.parse("^0.2.3")
    assert r.satisfies("0.2.9") and not r.satisfies("0.3.0")
    r = VersionRange.parse("^0.0.3")
    assert r.satisfies("0.0.3") and not r.satisfies("0.0.4")
    r = VersionRange.parse("^0")
    assert r.satisfies("0.9.9") and not r.satisfies("1.0.0")
    r = VersionRange.parse("^0.0")
    assert r.satisfies("0.0.9") and not r.satisfies("0.1.0")


def test_partial_comparators() -> None:
    assert VersionRange.parse("<1.2").satisfies("1.1.9")
    assert not VersionRange.parse("<1.2").satisfies("1.2.0")
    assert VersionRange.parse("<=1.2").satisfies("1.2.9")
    assert VersionRange.parse(">=1").satisfies("1.0.0")
    assert not VersionRange.parse(">*").satisfies("1.0.0")
    assert VersionRange.parse("1.2.3").is_exact
    assert not VersionRange.parse("^1").is_exact


@pytest.mark.parametrize("bad", ["^", "~x.1", "1.x.3", ">=a", "1 ||", "^1.2-rc", "x" * 300])
def test_invalid_ranges(bad: str) -> None:
    with pytest.raises(ValueError):
        VersionRange.parse(bad)


@given(versions, versions)
def test_caret_property(base: Version, v: Version) -> None:
    """For major > 0 and release versions, ^base == same major and >= base."""
    if base.major == 0 or base.prerelease or v.prerelease:
        return
    r = VersionRange.parse(f"^{base.major}.{base.minor}.{base.patch}")
    assert r.satisfies(v) == (v.major == base.major and v >= base)


@given(versions, st.lists(versions, max_size=8))
def test_max_satisfying_is_max(base: Version, avail: list[Version]) -> None:
    r = VersionRange.parse(f">={base.major}.{base.minor}.{base.patch}")
    best = r.max_satisfying(avail)
    ok = [v for v in avail if r.satisfies(v)]
    assert best == (max(ok) if ok else None)


# ---------------------------------------------------------------------------------- refs


def test_use_ref() -> None:
    r = UseRef.parse("core.resample@^1")
    assert r.id == "core.resample" and r.namespace == "core"
    assert str(r) == "core.resample@^1"
    assert str(UseRef.parse("core.ppc_fan")) == "core.ppc_fan"
    assert str(r.pinned("1.2.3")) == "core.resample@1.2.3"
    assert str(r.resolve(["1.0.0", "1.9.0", "2.0.0"])) == "1.9.0"
    t = UseRef.parse("t/acme-co/my_pkg.model@~2.1")
    assert t.namespace == "t/acme-co/my_pkg"


@pytest.mark.parametrize(
    "bad", ["resample@^1", "Core.resample", "core.resample@", "core..x", "t/../x.y", "core.x@^q"]
)
def test_use_ref_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        UseRef.parse(bad)


def test_dataset_ref() -> None:
    r = DatasetRef.parse("dataset://growth/eu_media_daily@latest")
    assert (r.project, r.name, r.version) == ("growth", "eu_media_daily", "latest")
    assert r.is_latest and not r.is_pinned and r.version_number is None
    assert DatasetRef.parse("dataset://shared/holidays_eu@v3").version_number == 3
    assert DatasetRef.parse("dataset://growth/plan_2027h1").is_latest
    pinned = r.pinned("a" * 64)
    assert pinned.is_pinned and str(pinned) == f"dataset://growth/eu_media_daily@sha256:{'a' * 64}"
    with pytest.raises(ValueError):
        r.pinned("xyz")
    for bad in ["dataset://growth", "http://x/y", "dataset://Growth/x", "dataset://a/b@sha256:12"]:
        with pytest.raises(ValueError):
            DatasetRef.parse(bad)


def test_other_refs() -> None:
    assert str(EndpointRef.parse("endpoint://growth/eu-geo-mmm-prod")) == (
        "endpoint://growth/eu-geo-mmm-prod"
    )
    m = ModelVersionRef.parse("model://growth/eu-geo-mmm@v3")
    assert m.version == "v3" and str(m) == "model://growth/eu-geo-mmm@v3"
    s = SpecRef.parse("spec://growth/eu-geo-mmm@r2")
    assert s.revision == "r2"
    for cls, bad in [
        (EndpointRef, "endpoint://growth"),
        (ModelVersionRef, "model://growth/x"),
        (SpecRef, "spec://growth/x@latest"),
    ]:
        with pytest.raises(ValueError):
            cls.parse(bad)  # type: ignore[attr-defined]


class _Holder(BaseModel):
    use: UseRef
    ds: DatasetRef
    target: ModelTarget


def test_refs_in_pydantic_models() -> None:
    h = _Holder.model_validate(
        {
            "use": "core.lag@^1",
            "ds": "dataset://p/n@v1",
            "target": "endpoint://p/alias",
        }
    )
    assert isinstance(h.use, UseRef) and isinstance(h.target, EndpointRef)
    dumped = h.model_dump(mode="json")
    assert dumped == {
        "use": "core.lag@^1",
        "ds": "dataset://p/n@v1",
        "target": "endpoint://p/alias",
    }
    assert _Holder.model_validate_json(h.model_dump_json()) == h
    h2 = _Holder.model_validate({**dumped, "target": "model://p/m@v2"})
    assert isinstance(h2.target, ModelVersionRef)
    with pytest.raises(ValidationError):
        _Holder.model_validate({**dumped, "target": "dataset://p/n"})
    with pytest.raises(ValidationError):
        _Holder.model_validate({**dumped, "use": 3})
    schema = _Holder.model_json_schema()
    assert schema["properties"]["use"]["type"] == "string"
    assert "pattern" in schema["properties"]["target"]
