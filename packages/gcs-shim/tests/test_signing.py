"""V4 signing: canonical forms, sign/verify round trips, tampering, expiry, POST policies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_gcs_shim._errors import ShimError
from mapi_gcs_shim.signing import (
    ALGORITHM,
    SigningIdentity,
    build_post_policy,
    canonical_query,
    canonical_request,
    derive_signing_key,
    hmac_policy_signer,
    resource_path,
    sign_url,
    string_to_sign,
    verify_post_policy,
    verify_signed_request,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
ALICE = SigningIdentity("alice@mapi-local.iam.gserviceaccount.com", "a" * 40)
BOB = SigningIdentity("bob@mapi-local.iam.gserviceaccount.com", "b" * 40)
SECRETS = {ALICE.access_id: ALICE.secret, BOB.access_id: BOB.secret}
ENDPOINT = "http://127.0.0.1:4443"
HOST = "127.0.0.1:4443"


def _split(url: str) -> tuple[str, list[tuple[str, str]]]:
    parts = urlsplit(url)
    return parts.path, parse_qsl(parts.query, keep_blank_values=True)


def _verify(
    url: str,
    *,
    method: str = "GET",
    bucket: str = "mapi-local-runs",
    name: str = "t/default/a b/c.zip",
    headers: dict[str, str] | None = None,
    now: datetime = NOW,
) -> object:
    _, query = _split(url)
    return verify_signed_request(
        method=method,
        bucket=bucket,
        name=name,
        query=query,
        headers={"host": HOST, **(headers or {})},
        lookup_secret=SECRETS.get,
        now=now,
    )


def _url(**kw: object) -> str:
    args: dict[str, object] = {
        "method": "GET",
        "endpoint": ENDPOINT,
        "bucket": "mapi-local-runs",
        "name": "t/default/a b/c.zip",
        "expires_in": timedelta(minutes=15),
        "now": NOW,
    }
    args.update(kw)
    return sign_url(ALICE, **args)  # type: ignore[arg-type]


# ------------------------------------------------------------------------- primitives


def test_signing_key_derivation_matches_the_goog4_chain() -> None:
    k = hmac.new(b"GOOG4" + b"s3cret", b"20261015", hashlib.sha256).digest()
    for part in (b"auto", b"storage", b"goog4_request"):
        k = hmac.new(k, part, hashlib.sha256).digest()
    assert derive_signing_key("s3cret", "20261015") == k


def test_canonical_query_sorts_encodes_and_drops_the_signature() -> None:
    q = canonical_query([("b", "x y"), ("a", "1/2"), ("X-Goog-Signature", "zz"), ("a", "0")])
    assert q == "a=0&a=1%2F2&b=x%20y"


def test_canonical_request_layout() -> None:
    creq = canonical_request(
        "put",
        "/b/o",
        [("k", "v")],
        {"Host": "h:1", "content-type": "  a   b "},
        ["content-type", "host"],
    )
    assert (
        creq == "PUT\n/b/o\nk=v\ncontent-type:a b\nhost:h:1\n\ncontent-type;host\nUNSIGNED-PAYLOAD"
    )
    sts = string_to_sign("20261015T093000Z", "20261015/auto/storage/goog4_request", creq)
    assert sts.splitlines()[0] == ALGORITHM
    assert sts.splitlines()[-1] == hashlib.sha256(creq.encode()).hexdigest()


def test_resource_path_keeps_slashes_and_encodes_the_rest() -> None:
    assert resource_path("b", "a b/c+d/é") == "/b/a%20b/c%2Bd/%C3%A9"
    assert resource_path("b") == "/b"


def test_signed_url_shape() -> None:
    url = _url(query={"generation": "17"})
    path, query = _split(url)
    params = dict(query)
    assert path == "/mapi-local-runs/t/default/a%20b/c.zip"
    assert params["X-Goog-Algorithm"] == ALGORITHM
    assert params["X-Goog-Credential"] == f"{ALICE.access_id}/20261015/auto/storage/goog4_request"
    assert params["X-Goog-Date"] == "20261015T093000Z"
    assert params["X-Goog-Expires"] == "900"
    assert params["X-Goog-SignedHeaders"] == "host"
    assert params["generation"] == "17"
    assert len(params["X-Goog-Signature"]) == 64


@pytest.mark.parametrize("bad", [timedelta(0), timedelta(days=7, seconds=1)])
def test_sign_url_rejects_bad_ttls(bad: timedelta) -> None:
    with pytest.raises(ValueError):
        _url(expires_in=bad)


@pytest.mark.parametrize("endpoint", ["ftp://h", "http://h/path", "http://h?q=1", "h:1"])
def test_sign_url_rejects_bad_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError):
        _url(endpoint=endpoint)


# ------------------------------------------------------------------------- round trip


def test_round_trip_with_headers_and_query() -> None:
    url = _url(
        method="PUT",
        headers={"content-type": "application/zip", "x-goog-if-generation-match": "0"},
        query={"generation": "3"},
    )
    info = _verify(
        url,
        method="PUT",
        headers={"content-type": "application/zip", "x-goog-if-generation-match": "0"},
    )
    assert info.access_id == ALICE.access_id  # type: ignore[attr-defined]
    assert info.expires_at == NOW + timedelta(minutes=15)  # type: ignore[attr-defined]


@settings(max_examples=60, deadline=None)
@given(
    name=st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=40
    ).filter(lambda s: "/" not in s and s not in (".", "..")),
    gen=st.integers(min_value=1, max_value=2**62),
)
def test_round_trip_any_object_name(name: str, gen: int) -> None:
    key = f"t/default/{name}"
    url = _url(name=key, query={"generation": str(gen)})
    _verify(url, name=key)


# ------------------------------------------------------------------------- tampering


def _mutate(url: str, key: str, value: str | None) -> str:
    path, query = _split(url)
    params = dict(query)
    if value is None:
        params.pop(key)
    else:
        params[key] = value
    return f"{ENDPOINT}{path}?{urlencode(sorted(params.items()), quote_via=quote)}"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("generation", "18"),
        ("X-Goog-Expires", "901"),
        ("X-Goog-Date", "20261015T093001Z"),
        ("extra", "1"),
    ],
)
def test_any_query_change_breaks_the_signature(key: str, value: str) -> None:
    url = _url(query={"generation": "17"})
    with pytest.raises(ShimError) as err:
        _verify(_mutate(url, key, value))
    assert err.value.status == 403 and err.value.xml_code == "SignatureDoesNotMatch"


def test_flipped_signature_byte_fails() -> None:
    url = _url()
    sig = dict(_split(url)[1])["X-Goog-Signature"]
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    with pytest.raises(ShimError, match="does not match"):
        _verify(_mutate(url, "X-Goog-Signature", flipped))


def test_path_method_and_host_are_bound() -> None:
    url = _url()
    with pytest.raises(ShimError):
        _verify(url, name="t/default/other.zip")
    with pytest.raises(ShimError):
        _verify(url, bucket="mapi-local-data")
    with pytest.raises(ShimError):
        _verify(url, method="PUT")
    with pytest.raises(ShimError):
        _verify(url, headers={"host": "evil.example:4443"})


def test_signed_header_values_are_bound_and_required() -> None:
    url = _url(method="PUT", headers={"content-type": "application/zip"})
    with pytest.raises(ShimError):
        _verify(url, method="PUT", headers={"content-type": "text/html"})
    with pytest.raises(ShimError, match="missing"):
        _verify(url, method="PUT")


def test_unsigned_goog_headers_are_rejected_except_x_goog_hash() -> None:
    url = _url(method="PUT")
    with pytest.raises(ShimError) as err:
        _verify(url, method="PUT", headers={"x-goog-if-generation-match": "5"})
    assert err.value.xml_code == "MissingSecurityHeader"
    _verify(url, method="PUT", headers={"x-goog-hash": "crc32c=AAAAAA=="})


def test_wrong_key_or_unknown_signer() -> None:
    forged = sign_url(
        SigningIdentity(ALICE.access_id, "wrong" * 8),
        method="GET",
        endpoint=ENDPOINT,
        bucket="mapi-local-runs",
        name="t/default/a b/c.zip",
        expires_in=timedelta(minutes=5),
        now=NOW,
    )
    with pytest.raises(ShimError, match="does not match"):
        _verify(forged)
    stranger = sign_url(
        SigningIdentity("mallory@evil.example", "m" * 40),
        method="GET",
        endpoint=ENDPOINT,
        bucket="mapi-local-runs",
        name="t/default/a b/c.zip",
        expires_in=timedelta(minutes=5),
        now=NOW,
    )
    with pytest.raises(ShimError, match="not a known service account"):
        _verify(stranger)


def test_credential_of_another_principal_does_not_verify() -> None:
    url = _url()
    cred = f"{BOB.access_id}/20261015/auto/storage/goog4_request"
    with pytest.raises(ShimError, match="does not match"):
        _verify(_mutate(url, "X-Goog-Credential", cred))


def test_expiry_and_not_yet_valid() -> None:
    url = _url(expires_in=timedelta(minutes=15))
    _verify(url, now=NOW + timedelta(minutes=15))  # inclusive bound
    with pytest.raises(ShimError) as err:
        _verify(url, now=NOW + timedelta(minutes=15, seconds=1))
    assert (err.value.status, err.value.xml_code) == (400, "ExpiredToken")
    _verify(url, now=NOW - timedelta(minutes=4))  # within the clock skew
    with pytest.raises(ShimError, match="not valid yet"):
        _verify(url, now=NOW - timedelta(minutes=6))


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("X-Goog-Algorithm", None, "missing"),
        ("X-Goog-Algorithm", "GOOG4-RSA-SHA256", "must be"),
        ("X-Goog-Credential", "alice/20261015/us/storage/goog4_request", "X-Goog-Credential"),
        ("X-Goog-Credential", f"{ALICE.access_id}/20261016/auto/storage/goog4_request", "date"),
        ("X-Goog-Date", "2026-10-15", "YYYYMMDD"),
        ("X-Goog-Expires", "604801", "604800"),
        ("X-Goog-Expires", "abc", "604800"),
        ("X-Goog-SignedHeaders", "content-type", "host"),
        ("X-Goog-SignedHeaders", "host;content-type", "sorted"),
        ("X-Goog-Signature", "nothex", "malformed"),
    ],
)
def test_malformed_parameters(key: str, value: str | None, match: str) -> None:
    with pytest.raises(ShimError, match=match):
        _verify(_mutate(_url(), key, value))


def test_duplicate_signing_parameters_are_rejected() -> None:
    _, query = _split(_url())
    query.append(("X-Goog-Expires", "60"))
    with pytest.raises(ShimError, match="duplicate"):
        verify_signed_request(
            method="GET",
            bucket="mapi-local-runs",
            name="t/default/a b/c.zip",
            query=query,
            headers={"host": HOST},
            lookup_secret=SECRETS.get,
            now=NOW,
        )


def test_identity_repr_hides_the_secret() -> None:
    assert ALICE.secret not in repr(ALICE)


# ------------------------------------------------------------------------- POST policy


def _policy(**kw: object) -> dict[str, str]:
    args: dict[str, object] = {
        "access_id": ALICE.access_id,
        "bucket": "mapi-local-runs",
        "prefix": "t/default/runs/r/a1/",
        "min_bytes": 1,
        "max_bytes": 100,
        "expires_in": timedelta(hours=1),
        "sign": hmac_policy_signer(ALICE.secret),
        "now": NOW,
    }
    args.update(kw)
    fields, _ = build_post_policy(**args)  # type: ignore[arg-type]
    return fields


def _check(
    fields: dict[str, str], *, bucket: str = "mapi-local-runs", now: datetime = NOW
) -> object:
    return verify_post_policy(fields, bucket=bucket, lookup_secret=SECRETS.get, now=now)


def test_policy_round_trip() -> None:
    fields = {**_policy(), "key": "t/default/runs/r/a1/out.zip", "content-type": "application/zip"}
    decision = _check(fields)
    assert decision.content_length_range == (1, 100)  # type: ignore[attr-defined]
    doc = json.loads(base64.b64decode(fields["policy"]))
    assert {"bucket": "mapi-local-runs"} in doc["conditions"]
    assert ["starts-with", "$key", "t/default/runs/r/a1/"] in doc["conditions"]
    assert doc["expiration"] == "2026-10-15T10:30:00Z"


def test_policy_violations() -> None:
    base = {**_policy(), "key": "t/default/runs/r/a1/out.zip"}
    with pytest.raises(ShimError, match="violates"):
        _check({**base, "key": "t/default/runs/OTHER/out.zip"})
    with pytest.raises(ShimError, match="bucket"):
        _check(base, bucket="mapi-local-bundles")
    with pytest.raises(ShimError, match="not covered"):
        _check({**base, "acl": "public-read"})
    _check({**base, "x-ignore-anything": "1"})
    with pytest.raises(ShimError) as err:
        _check(base, now=NOW + timedelta(hours=1, seconds=1))
    assert err.value.xml_code == "ExpiredToken"
    with pytest.raises(ShimError, match="missing"):
        _check({k: v for k, v in base.items() if k != "key"})


def test_policy_tampering() -> None:
    base = {**_policy(), "key": "t/default/runs/r/a1/out.zip"}
    doc = json.loads(base64.b64decode(base["policy"]))
    doc["conditions"] = [
        c for c in doc["conditions"] if not (isinstance(c, list) and c[0] == "content-length-range")
    ]
    widened = base64.b64encode(json.dumps(doc).encode()).decode()
    with pytest.raises(ShimError, match="signature does not match"):
        _check({**base, "policy": widened})
    with pytest.raises(ShimError, match="does not match"):
        _check({**base, "x-goog-date": "20261015T093001Z"})
    forged = _policy(sign=hmac_policy_signer("wrong" * 8))
    with pytest.raises(ShimError, match="signature does not match"):
        _check({**forged, "key": "t/default/runs/r/a1/out.zip"})


def test_build_post_policy_validates_arguments() -> None:
    with pytest.raises(ValueError):
        _policy(min_bytes=10, max_bytes=1)
    with pytest.raises(ValueError):
        _policy(expires_in=timedelta(days=8))
