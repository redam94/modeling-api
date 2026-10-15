"""API tokens: format, checksum, hashing, scopes and the DTOs that must never leak secrets."""

from __future__ import annotations

import itertools
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import Action, ResourceKind
from mapi_spec.tokens import (
    TOKEN_ALPHABET,
    TOKEN_RE,
    ApiToken,
    ParsedToken,
    ScopeName,
    TokenCreate,
    TokenCreated,
    TokenRecord,
    TokenStatus,
    bearer_token,
    generate_token,
    hash_token,
    looks_like_token,
    parse_scope,
    parse_token,
    redact_token,
    scope_allows,
    token_checksum,
    verify_token_hash,
)
from pydantic import ValidationError

PEPPER = b"test-pepper-not-secret"
FIXED = ParsedToken(
    "7k2m9q4x",
    "0123456789abcdefghjkmnpqrstvwxyz",
    token_checksum("7k2m9q4x", "0123456789abcdefghjkmnpqrstvwxyz"),
)


def test_generated_tokens_have_the_documented_shape() -> None:
    t = generate_token()
    s = str(t)
    assert TOKEN_RE.fullmatch(s)
    assert s.startswith("mapi_") and len(s) == 5 + 8 + 1 + 32 + 7
    assert set(t.prefix + t.secret + t.checksum) <= set(TOKEN_ALPHABET)
    assert not set("ilou") & set(s.removeprefix("mapi_"))
    assert parse_token(s) == t
    assert generate_token() != t


def test_checksum_is_crc32_of_the_body_in_base32() -> None:
    body = f"mapi_{FIXED.prefix}_{FIXED.secret}".encode()
    value = zlib.crc32(body)
    digits = "".join(TOKEN_ALPHABET[(value >> (5 * i)) & 31] for i in reversed(range(7)))
    assert FIXED.checksum == digits
    assert str(FIXED) == "mapi_7k2m9q4x_0123456789abcdefghjkmnpqrstvwxyz" + digits


def test_every_single_character_substitution_is_detected() -> None:
    s = str(FIXED)
    body_start = len("mapi_")
    for i in range(body_start, len(s)):
        if s[i] == "_":
            continue
        for c in TOKEN_ALPHABET:
            if c == s[i]:
                continue
            with pytest.raises(MapiError):
                parse_token(s[:i] + c + s[i + 1 :])


def test_adjacent_transpositions_are_detected() -> None:
    s = str(FIXED)
    for i in range(len("mapi_"), len(s) - 1):
        if s[i] == s[i + 1] or "_" in (s[i], s[i + 1]):
            continue
        swapped = s[:i] + s[i + 1] + s[i] + s[i + 2 :]
        with pytest.raises(MapiError):
            parse_token(swapped)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "mapi_",
        "Bearer mapi_7k2m9q4x_0123",
        str(FIXED).upper(),  # tokens are lowercase only
        "mapi_7k2m9q4i_0123456789abcdefghjkmnpqrstvwxyz0000000",
        "xapi_7k2m9q4x_0123456789abcdefghjkmnpqrstvwxyz0000000",
        "mapi_7k2m9q4x_0123456789abcdefghjkmnpqrstvwxyz0000000",
        "x" * 10_000,
    ],
)
def test_malformed_tokens_are_unauthenticated_and_not_echoed(bad: str) -> None:
    with pytest.raises(MapiError) as exc:
        parse_token(bad)
    assert exc.value.code is ErrorCode.UNAUTHENTICATED
    assert bad[5:] not in exc.value.detail or len(bad) < 6


def test_redaction_never_shows_the_secret() -> None:
    assert FIXED.display == redact_token(FIXED.prefix) == "mapi_7k2m9q4x_…"
    assert FIXED.secret not in repr(FIXED)
    with pytest.raises(ValueError):
        generate_token("TOOLONGPREFIX")


def test_hash_is_peppered_and_constant_time_verifiable() -> None:
    h = hash_token(FIXED, PEPPER)
    assert h.startswith("hmac-sha256:") and len(h) == len("hmac-sha256:") + 64
    assert hash_token(str(FIXED), PEPPER) == h
    assert hash_token(FIXED, b"other pepper") != h
    assert verify_token_hash(str(FIXED), h, PEPPER)
    assert not verify_token_hash(str(generate_token()), h, PEPPER)
    assert FIXED.secret not in h


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer mapi_x", "mapi_x"),
        ("bearer   mapi_x ", "mapi_x"),
        ("Basic abc", None),
        ("Bearer", None),
        ("Bearer   ", None),
    ],
)
def test_bearer_header(header: str | None, expected: str | None) -> None:
    assert bearer_token(header) == expected


def test_secret_scanning() -> None:
    assert looks_like_token(f"leaked in a log line: {FIXED} oops")
    assert not looks_like_token("mapi_7k2m9q4x_0123456789abcdefghjkmnpqrstvwxyz0000000")
    assert not looks_like_token("nothing here")


@given(st.text(alphabet=TOKEN_ALPHABET, min_size=8, max_size=8))
def test_round_trip_any_prefix(prefix: str) -> None:
    t = generate_token(prefix)
    assert parse_token(str(t)) == t and t.prefix == prefix


# ------------------------------------------------------------------------------------ scopes


@pytest.mark.parametrize(
    ("scope", "name", "target"),
    [
        ("models:read", ScopeName.MODELS_READ, None),
        ("runs:create", ScopeName.RUNS_CREATE, None),
        ("score:geo-mmm-prod", ScopeName.SCORE, "geo-mmm-prod"),
        ("score:*", ScopeName.SCORE, "*"),
        ("plugins:publish", ScopeName.PLUGINS_PUBLISH, None),
        ("datasets:write", ScopeName.DATASETS_WRITE, None),
        ("admin", ScopeName.ADMIN, None),
    ],
)
def test_parse_scope(scope: str, name: ScopeName, target: str | None) -> None:
    assert parse_scope(scope) == (name, target)


@pytest.mark.parametrize(
    "bad", ["score", "score:", "score:Bad", "score:a\n", "models:write", "ADMIN", "x"]
)
def test_parse_scope_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_scope(bad)


SCOPE_EXPECTED: dict[str, set[tuple[str, str]]] = {
    "models:read": {
        (r, "read")
        for r in (
            "catalog",
            "dataset",
            "spec",
            "run",
            "model",
            "endpoint",
            "plot",
            "usage",
            "project",
        )
    },
    "datasets:write": {("dataset", "read"), ("dataset", "create"), ("dataset", "update")},
    "plugins:publish": {("plugin", "read"), ("plugin", "create"), ("plugin", "delete")},
}
SCOPE_EXPECTED["runs:create"] = SCOPE_EXPECTED["models:read"] | {
    ("run", "create"),
    ("run", "cancel"),
    ("model", "run"),
    ("endpoint", "run"),
    ("plot", "run"),
}


@pytest.mark.parametrize("scope", sorted(SCOPE_EXPECTED))
def test_scope_grants_exhaustively(scope: str) -> None:
    for action, resource in itertools.product(Action, ResourceKind):
        want = (resource.value, action.value) in SCOPE_EXPECTED[scope]
        assert scope_allows([scope], action.value, resource.value) is want, (action, resource)


def test_score_scope_targets_one_endpoint_and_admin_covers_all() -> None:
    assert scope_allows(["score:a"], "run", "endpoint", resource_name="a")
    assert scope_allows(["score:a"], "read", "endpoint", resource_name="a")
    assert not scope_allows(["score:a"], "run", "endpoint", resource_name="b")
    assert not scope_allows(["score:a"], "run", "endpoint")
    # until endpoints exist, score:<model id> scores that model (score:* every model)
    assert scope_allows(["score:a"], "run", "model", resource_name="a")
    assert not scope_allows(["score:a"], "run", "model", resource_name="b")
    assert not scope_allows(["score:a"], "run", "model")
    assert scope_allows(["score:*"], "run", "model")
    assert not scope_allows(["score:*"], "read", "model")
    assert not scope_allows(["score:*"], "create", "run")
    assert scope_allows(["score:*"], "run", "endpoint", resource_name="anything")
    assert scope_allows(["admin"], "delete", "dataset")
    assert not scope_allows([], "read", "model")
    assert not scope_allows(["bogus"], "read", "model")


def test_no_scope_lets_a_token_mint_tokens() -> None:
    """A child token would escape its parent's expiry, budget, rate limit and revocation,
    so minting needs a signed-in session whatever the token's scopes."""
    for scope in [*SCOPE_EXPECTED, "admin", "score:*"]:
        assert not scope_allows([scope], "create", "token"), scope
    assert scope_allows(["admin"], "delete", "token")  # revoking stays possible


# -------------------------------------------------------------------------------------- DTOs

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)


def _record(**kw: Any) -> TokenRecord:
    base: dict[str, Any] = {
        "id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
        "tenant_id": "acme",
        "project_id": "growth",
        "owner_id": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
        "name": "ci",
        "prefix": FIXED.prefix,
        "secret_hash": hash_token(FIXED, PEPPER),
        "scopes": ["models:read"],
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=30),
    }
    base.update(kw)
    return TokenRecord(**base)


def test_token_create_validates_scopes_and_limits() -> None:
    tc = TokenCreate(name="ci", scopes=["models:read", "score:geo"])
    assert tc.expires_in_days == 90 and tc.budget_period == "month"
    for bad in (
        {"name": "ci", "scopes": []},
        {"name": "ci", "scopes": ["models:read", "models:read"]},
        {"name": "ci", "scopes": ["root"]},
        {"name": "ci", "scopes": ["admin"], "expires_in_days": 366},
        {"name": "c\ni", "scopes": ["admin"]},
        {"name": "ci", "scopes": ["admin"], "secret": "x"},
    ):
        with pytest.raises(ValidationError):
            TokenCreate.model_validate(bad)


def test_public_views_never_carry_the_hash_or_secret() -> None:
    rec = _record()
    public = rec.to_public(NOW)
    dumped = public.model_dump_json()
    assert "secret" not in ApiToken.model_fields and "secret_hash" not in ApiToken.model_fields
    assert rec.secret_hash not in dumped and FIXED.secret not in dumped
    assert public.display == "mapi_7k2m9q4x_…" and public.status is TokenStatus.ACTIVE
    assert "secret_hash=<redacted>" in repr(rec) and rec.secret_hash not in repr(rec)
    created = TokenCreated(token=public, secret=str(FIXED))
    assert FIXED.secret not in repr(created) and FIXED.secret not in str(created)
    with pytest.raises(ValidationError):
        TokenCreated(token=public, secret="not-a-token")


def test_token_status_over_time() -> None:
    rec = _record()
    assert rec.status_at(NOW) is TokenStatus.ACTIVE and rec.usable_at(NOW)
    assert rec.status_at(NOW + timedelta(days=30)) is TokenStatus.EXPIRED
    revoked = _record(revoked_at=NOW + timedelta(days=1))
    assert revoked.status_at(NOW) is TokenStatus.ACTIVE
    assert revoked.status_at(NOW + timedelta(days=2)) is TokenStatus.REVOKED
    with pytest.raises(ValidationError, match="after created_at"):
        _record(expires_at=NOW)
