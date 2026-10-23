"""v0.20 dashboard read-token scope: ``dashboard:<dashboard_id>`` (plan D12, I-2)."""

from __future__ import annotations

import pytest
from mapi_spec.tenancy import PERMISSIONS
from mapi_spec.tokens import (
    TOKEN_NEVER,
    ScopeName,
    TokenCreate,
    check_scope,
    dashboard_scope,
    dashboard_scope_target,
    parse_scope,
    scope_allows,
    validate_scopes,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

DSH = "dsh_01jaf3k1m2n3p4q5r6s7t8v9w0"
OTHER = "dsh_01jaf3k1m2n3p4q5r6s7t8v9w1"


def test_parse_scope_takes_a_dashboard_target() -> None:
    assert ScopeName.DASHBOARD.value == "dashboard"
    assert parse_scope(f"dashboard:{DSH}") == (ScopeName.DASHBOARD, DSH)
    assert dashboard_scope(DSH) == f"dashboard:{DSH}"
    assert check_scope(dashboard_scope(DSH)) == f"dashboard:{DSH}"


@pytest.mark.parametrize(
    "bad",
    [
        "dashboard",  # no target
        "dashboard:",  # empty target
        f"dashboard:{DSH}:read",  # the outline's form: not a valid id
        "dashboard:*",  # no wildcard
        "dashboard:../x",
        "dashboard: dsh_1",
        "dashboard:" + "a" * 129,
        "dashboards:dsh_1",
    ],
)
def test_parse_scope_refuses_bad_dashboard_scopes(bad: str) -> None:
    with pytest.raises(ValueError, match="unknown scope"):
        parse_scope(bad)
    with pytest.raises(ValidationError):
        TokenCreate(name="t", scopes=[bad])


def test_dashboard_scope_refuses_an_invalid_id() -> None:
    with pytest.raises(ValueError):
        dashboard_scope("bad id")


def test_scope_allows_only_read_on_the_matching_dashboard() -> None:
    scopes = [dashboard_scope(DSH)]
    assert scope_allows(scopes, "read", "dashboard", resource_name=DSH)
    assert not scope_allows(scopes, "read", "dashboard", resource_name=OTHER)
    assert not scope_allows(scopes, "read", "dashboard")  # no name: no match
    for resource, action in PERMISSIONS:
        if (resource.value, action.value) == ("dashboard", "read"):
            continue
        for name in (DSH, None):
            assert not scope_allows(scopes, action.value, resource.value, resource_name=name), (
                resource,
                action,
            )
    # the score family is unchanged by the new target family
    assert not scope_allows(scopes, "run", "endpoint", resource_name=DSH)
    assert not scope_allows(scopes, "run", "dashboard", resource_name=DSH)


def test_token_never_is_unchanged() -> None:
    assert (
        frozenset(
            {
                ("token", "create"),
                ("endpoint", "manage"),
                ("model", "approve"),
                ("dashboard_grant", "create"),
                ("dashboard_share", "create"),
                ("plugin", "approve"),
                ("plugin", "delete"),
                ("plugin_settings", "update"),
            }
        )
        == TOKEN_NEVER
    )


def test_a_dashboard_scope_is_alone_on_its_token() -> None:
    assert validate_scopes([dashboard_scope(DSH)]) == [dashboard_scope(DSH)]
    assert validate_scopes(["models:read", "score:*"]) == ["models:read", "score:*"]
    for mixed in (
        [dashboard_scope(DSH), "models:read"],
        ["admin", dashboard_scope(DSH)],
        [dashboard_scope(DSH), dashboard_scope(OTHER)],
    ):
        with pytest.raises(ValueError, match="only scope"):
            validate_scopes(mixed)
        with pytest.raises(ValidationError, match="only scope"):
            TokenCreate(name="t", scopes=mixed)
    with pytest.raises(ValidationError, match="unique"):
        TokenCreate(name="t", scopes=["models:read", "models:read"])


def test_dashboard_scope_target() -> None:
    assert dashboard_scope_target([dashboard_scope(DSH)]) == DSH
    assert dashboard_scope_target(("models:read", "score:*")) is None
    assert dashboard_scope_target([]) is None
    assert dashboard_scope_target(["dashboard", "nonsense"]) is None
