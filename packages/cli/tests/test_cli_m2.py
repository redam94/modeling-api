"""v0.3 commands against a respx-mocked API: login/logout/whoami/use, tokens, usage, admin,
run cost, quota errors and the config writer. No command may print a secret."""

from __future__ import annotations

import base64
import json
import stat
import time
import tomllib
import urllib.request
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import respx
from mapi_spec.api import (
    ApiToken,
    AuditEvent,
    ExecutorHealth,
    KillSwitches,
    Me,
    QuotaPolicy,
    Run,
    Tenant,
    TokenCreated,
    UsageReport,
)
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.tokens import generate_token
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]
Ex = Callable[..., dict[str, Any]]

URL = "https://api.test"
V1 = f"{URL}/v1"
ISSUER = "https://idp.test"
HEADER = "X-Mapi-Project"
TOKEN = str(generate_token())
SECRET_PART = TOKEN[14:]  # everything after mapi_<prefix>_


def page(items: list[Any]) -> dict[str, Any]:
    return {"items": items, "next_cursor": None}


def problem(code: ErrorCode, detail: str) -> dict[str, Any]:
    return Problem.from_code(code, detail).model_dump(mode="json")


def token_me(example: Ex, *, tenant: str = "acme", project: str = "growth") -> dict[str, Any]:
    me = example(Me)
    me["auth"] = "token"
    me["principal"] = {
        "kind": "token",
        "id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
        "prefix": "7k2m9q4x",
        "owner_id": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
        "tenant_id": tenant,
        "project_id": project,
        "scopes": ["admin"],
    }
    me["token"] = example(ApiToken)
    return me


def config_of(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())


def assert_no_secret(*results: Result) -> None:
    for res in results:
        assert SECRET_PART not in res.stdout and SECRET_PART not in res.stderr


def write_config(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


# ------------------------------------------------------------------------- login: token


def test_login_with_token_verifies_and_stores_privately(
    mapi: Invoke, api: respx.MockRouter, example: Ex, isolated_env: Path
) -> None:
    me = api.get(f"{V1}/me").respond(200, json=token_me(example))
    res = mapi("--url", URL, "--profile", "prod", "login", "--with-token", input=TOKEN + "\n")
    assert res.exit_code == 0, res.output
    assert me.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "Logged in to https://api.test" in res.stdout
    assert TOKEN[:14] + "…" in res.stdout  # the redacted form only
    assert_no_secret(res)
    assert stat.S_IMODE(isolated_env.stat().st_mode) == 0o600
    prof = config_of(isolated_env)["profiles"]["prod"]
    assert prof == {"url": URL, "token": TOKEN}
    # the stored token is used from now on
    runs = api.get(f"{V1}/runs").respond(200, json=page([]))
    assert mapi("--profile", "prod", "runs", "ls").exit_code == 0
    assert runs.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_login_with_token_rejects_typos_and_bad_tokens(
    mapi: Invoke, api: respx.MockRouter, isolated_env: Path
) -> None:
    typo = TOKEN[:-1] + ("0" if TOKEN[-1] != "0" else "1")
    res = mapi("--url", URL, "login", "--with-token", input=typo + "\n")
    assert res.exit_code == 1 and "checksum" in res.stderr
    assert typo[14:] not in res.output
    api.get(f"{V1}/me").respond(401, json=problem(ErrorCode.UNAUTHENTICATED, "revoked"))
    res = mapi("--url", URL, "login", "--with-token", input=TOKEN + "\n")
    assert res.exit_code == 1 and "UNAUTHENTICATED" in res.stderr
    assert "token" not in config_of(isolated_env) if isolated_env.exists() else True
    assert_no_secret(res)
    res = mapi("--url", URL, "login", "--with-token", input="\n")
    assert res.exit_code == 1 and "no token on stdin" in res.stderr


def test_login_creates_a_new_named_profile(
    mapi: Invoke, api: respx.MockRouter, example: Ex, isolated_env: Path
) -> None:
    api.get("http://127.0.0.1:8000/v1/me").respond(200, json=token_me(example))
    res = mapi("--profile", "fresh", "login", "--with-token", input=TOKEN + "\n")
    assert res.exit_code == 0, res.output
    assert config_of(isolated_env)["profiles"]["fresh"] == {"token": TOKEN}


def test_login_with_token_on_a_v02_server_warns(
    mapi: Invoke, api: respx.MockRouter, isolated_env: Path
) -> None:
    api.get(f"{V1}/me").respond(404, json=problem(ErrorCode.NOT_FOUND, "no route"))
    res = mapi("--url", URL, "login", "--with-token", input=TOKEN + "\n")
    assert res.exit_code == 0 and "unverified" in res.stderr
    assert config_of(isolated_env)["token"] == TOKEN


# -------------------------------------------------------------------------- login: OIDC


def _jwt(**claims: Any) -> str:
    def b64(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    base = {
        "iss": ISSUER,
        "aud": "mapi-cli",
        "sub": "110169484474386276334",
        "email": "ana@acme.example",
        "exp": int(time.time()) + 3600,
    }
    return f"{b64({'alg': 'RS256'})}.{b64({**base, **claims})}.c2ln"


@pytest.fixture
def idp(api: respx.MockRouter) -> dict[str, str]:
    seen: dict[str, str] = {}
    api.get(f"{ISSUER}/.well-known/openid-configuration").respond(
        200,
        json={
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "device_authorization_endpoint": f"{ISSUER}/device",
            "revocation_endpoint": f"{ISSUER}/revoke",
        },
    )

    def token(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        nonce = seen.get("nonce") if form["grant_type"] == "authorization_code" else None
        claims = {"nonce": nonce} if nonce else {}
        return httpx.Response(200, json={"id_token": _jwt(**claims), "refresh_token": "rt"})

    api.post(f"{ISSUER}/token").mock(side_effect=token)
    return seen


def test_browser_login_stores_the_session(
    mapi: Invoke,
    api: respx.MockRouter,
    example: Ex,
    idp: dict[str, str],
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def browser(url: str) -> bool:
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        idp["nonce"] = q["nonce"]
        query = urlencode({"code": "c", "state": q["state"]})
        urllib.request.urlopen(f"{q['redirect_uri']}?{query}").read()
        return True

    monkeypatch.setattr("mapi_cli.commands.auth.webbrowser.open", browser)
    me = api.get(f"{V1}/me").respond(200, json=example(Me))
    res = mapi("--url", URL, "login", "--issuer", ISSUER)
    assert res.exit_code == 0, res.output
    assert "as ana@acme.example" in res.stdout
    assert "Opening the browser" in res.stderr and f"{ISSUER}/authorize?" in res.stderr
    assert me.calls.last.request.headers["Authorization"].startswith("Bearer ey")
    creds = isolated_env.parent / "credentials.json"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    stored = json.loads(creds.read_text())["profiles"]["default"]
    assert stored["refresh_token"] == "rt" and stored["api_url"] == URL
    assert stored["id_token"] not in res.output and "rt" not in res.stdout.split()
    assert config_of(isolated_env) == {"url": URL, "oidc_issuer": ISSUER}
    # whoami uses the stored session and shows when it expires
    who = mapi("whoami")
    assert who.exit_code == 0, who.output
    assert "ana@acme.example" in who.stdout and "session expires" in who.stdout
    # config show describes the sign-in, never its tokens
    show = mapi("config", "show")
    assert "sign-in ana@acme.example" in show.stdout and stored["id_token"] not in show.stdout
    # logout revokes the refresh token and forgets the session
    revoke = api.post(f"{ISSUER}/revoke").respond(200)
    out = mapi("logout")
    assert out.exit_code == 0 and "Logged out" in out.stdout
    assert revoke.called and not creds.exists()


def test_device_login(
    mapi: Invoke,
    api: respx.MockRouter,
    example: Ex,
    idp: dict[str, str],
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mapi_client.oidc import login as real_login

    monkeypatch.setattr(
        "mapi_cli.commands.auth.oidc_login", partial(real_login, sleep=lambda s: None)
    )
    api.post(f"{ISSUER}/device").respond(
        200,
        json={
            "device_code": "d",
            "user_code": "\x1b[31mABCD-EFGH",
            "verification_uri": "https://idp.test/activate",
            "expires_in": 600,
            "interval": 5,
        },
    )
    api.get(f"{V1}/me").respond(200, json=example(Me))
    write_config(isolated_env, f'[profiles.p]\nurl = "{URL}"\noidc_issuer = "{ISSUER}"\n')
    res = mapi("--profile", "p", "login", "--device")
    assert res.exit_code == 0, res.output
    assert "ABCD-EFGH" in res.stderr and "\x1b" not in res.stderr  # IdP text is sanitized
    assert "https://idp.test/activate" in res.stderr
    creds = json.loads((isolated_env.parent / "credentials.json").read_text())
    assert "p" in creds["profiles"]


def test_login_without_an_issuer_explains(mapi: Invoke) -> None:
    res = mapi("--url", URL, "login")
    assert res.exit_code == 1 and "no OIDC issuer" in res.stderr


# ------------------------------------------------------------------------ whoami / use


def test_whoami_for_a_token(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/me").respond(200, json=token_me(example))
    res = mapi("--url", URL, "--token", TOKEN, "whoami")
    assert res.exit_code == 0, res.output
    assert "bound to" in res.stdout and "acme/growth" in res.stdout
    assert "mapi_7k2m9q4x_…" in res.stdout
    assert_no_secret(res)
    js = mapi("--url", URL, "--token", TOKEN, "--json", "whoami")
    doc = json.loads(js.stdout)
    assert doc["me"]["auth"] == "token" and doc["credentials"] == "token"
    assert_no_secret(js)


def test_a_token_bound_elsewhere_is_flagged(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    api.get(f"{V1}/me").respond(200, json=token_me(example))
    res = mapi("--url", URL, "--token", TOKEN, "--project", "acme/retail", "whoami")
    assert res.exit_code == 0 and "bound to acme/growth" in res.stderr
    ok = mapi("--url", URL, "--token", TOKEN, "--project", "growth", "whoami")
    assert "bound to" not in ok.stderr


def test_whoami_unauthenticated_hints_login(mapi: Invoke, api: respx.MockRouter) -> None:
    api.get(f"{V1}/me").respond(401, json=problem(ErrorCode.UNAUTHENTICATED, "sign in"))
    res = mapi("--url", URL, "whoami")
    assert res.exit_code == 1 and "mapi login" in res.stderr


def test_use_selects_and_verifies_the_project(
    mapi: Invoke, api: respx.MockRouter, example: Ex, isolated_env: Path
) -> None:
    me = api.get(f"{V1}/me").respond(200, json=example(Me))
    res = mapi("--url", URL, "use", "growth")  # bare id, one tenant has it
    assert res.exit_code == 0, res.output
    assert "Using project acme/growth" in res.stdout
    assert HEADER not in me.calls.last.request.headers
    assert config_of(isolated_env)["project"] == "acme/growth"
    runs = api.get(f"{V1}/runs").respond(200, json=page([]))
    assert mapi("--url", URL, "runs", "ls").exit_code == 0
    assert runs.calls.last.request.headers[HEADER] == "acme/growth"
    # --project overrides the selection for one command
    assert mapi("--url", URL, "--project", "acme/other", "runs", "ls").exit_code == 0
    assert runs.calls.last.request.headers[HEADER] == "acme/other"
    shown = mapi("--json", "use")
    assert json.loads(shown.stdout)["project"] == "acme/growth"
    bad = mapi("--url", URL, "use", "acme/nope")
    assert bad.exit_code == 1 and "not a member" in bad.stderr and "acme/growth" in bad.stderr
    invalid = mapi("--url", URL, "use", "Not A Slug")
    assert invalid.exit_code == 1 and "invalid project" in invalid.stderr
    cleared = mapi("use", "--unset")
    assert cleared.exit_code == 0 and "project" not in config_of(isolated_env)
    offline = mapi("use", "acme/x", "--no-verify")
    assert offline.exit_code == 0 and config_of(isolated_env)["project"] == "acme/x"


def test_use_refuses_projects_a_token_cannot_reach(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    me = token_me(example, project="retail")
    me["tenants"][0]["projects"].append(
        {"tenant_id": "acme", "project_id": "retail", "name": "Retail", "role": "viewer"}
    )
    api.get(f"{V1}/me").respond(200, json=me)
    res = mapi("--url", URL, "--token", TOKEN, "use", "acme/growth")
    assert res.exit_code == 1 and "bound to acme/retail" in res.stderr


def test_logout_with_revoke(
    mapi: Invoke, api: respx.MockRouter, example: Ex, isolated_env: Path
) -> None:
    write_config(isolated_env, f'[profiles.ci]\nurl = "{URL}"\ntoken = "{TOKEN}"\n')
    api.get(f"{V1}/me").respond(200, json=token_me(example))
    revoke = api.delete(f"{V1}/tokens/tok_01jaf3r1s2t3v4w5x6y7z8a9b0").respond(204)
    res = mapi("--profile", "ci", "logout", "--revoke")
    assert res.exit_code == 0, res.output
    assert revoke.called and "revoked token" in res.stderr
    assert config_of(isolated_env)["profiles"]["ci"] == {"url": URL}
    again = mapi("--profile", "ci", "logout")
    assert "no stored credentials" in again.stderr


# ------------------------------------------------------------------------------ tokens


def test_tokens_create_prints_the_secret_once_to_stdout(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    created = example(TokenCreated)
    secret = created["secret"]
    route = api.post(f"{V1}/tokens").respond(201, json=created)
    res = mapi(
        "--url", URL, "--project", "acme/growth", "tokens", "create", "atlas",
        "-s", "score:geo-mmm-prod", "-s", "models:read", "--expires-in-days", "30",
        "--rate-limit", "120", "--budget", "36000",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == secret  # stdout is exactly the token (pipe-friendly)
    assert secret not in res.stderr and "not shown again" in res.stderr
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "name": "atlas",
        "scopes": ["score:geo-mmm-prod", "models:read"],
        "expires_in_days": 30,
        "rate_limit_per_minute": 120,
        "vcpu_seconds_budget": 36000.0,
        "budget_period": "month",
    }
    assert route.calls.last.request.headers[HEADER] == "acme/growth"


def test_tokens_create_save_keeps_the_secret_off_the_terminal(
    mapi: Invoke, api: respx.MockRouter, example: Ex, isolated_env: Path
) -> None:
    created = example(TokenCreated)
    api.post(f"{V1}/tokens").respond(201, json=created)
    res = mapi("--url", URL, "tokens", "create", "ci", "-s", "runs:create", "--save")
    assert res.exit_code == 0, res.output
    assert created["secret"] not in res.output
    assert config_of(isolated_env)["token"] == created["secret"]
    assert stat.S_IMODE(isolated_env.stat().st_mode) == 0o600


def test_tokens_ls_get_and_revoke_with_confirmation(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    listing = api.get(f"{V1}/tokens").respond(200, json=page([example(ApiToken)]))
    res = mapi("--url", URL, "tokens", "ls", "--all", "--status", "active")
    assert res.exit_code == 0 and "mapi_7k2m9q4x_…" in res.stdout
    assert listing.calls.last.request.url.params["all"] == "true"
    api.get(f"{V1}/tokens/tok_1").respond(200, json=example(ApiToken))
    assert "atlas-optimiser" in mapi("--url", URL, "tokens", "get", "tok_1").stdout
    revoke = api.delete(f"{V1}/tokens/tok_1").respond(204)
    declined = mapi("--url", URL, "tokens", "revoke", "tok_1", input="n\n")
    assert declined.exit_code == 1 and not revoke.called
    accepted = mapi("--url", URL, "tokens", "revoke", "tok_1", input="y\n")
    assert accepted.exit_code == 0 and revoke.call_count == 1
    assert "Revoke 'atlas-optimiser'" in accepted.stderr
    scripted = mapi("--url", URL, "--json", "tokens", "revoke", "tok_1", "--yes")
    assert json.loads(scripted.stdout) == {"revoked": "tok_1"} and revoke.call_count == 2
    no_input = mapi("--url", URL, "tokens", "revoke", "tok_1")
    assert no_input.exit_code == 1 and revoke.call_count == 2


# ------------------------------------------------------------------------- usage / cost


def test_usage_shows_cost_and_quotas(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson
) -> None:
    route = api.get(f"{V1}/usage").respond(200, json=example(UsageReport))
    res = mapi("--url", URL, "--project", "acme/growth", "usage", "--group-by", "resource_class")
    assert res.exit_code == 0, res.output
    assert "0.093600 USD" in res.stdout and "vcpu_seconds_per_day" in res.stdout
    assert "50%" in res.stdout and "cloudrun-list-2026-10" in res.stdout
    assert route.calls.last.request.url.params["group_by"] == "resource_class"
    tenant = api.get(f"{V1}/tenants/acme/usage").respond(200, json=example(UsageReport))
    js = mapi("--url", URL, "--json", "usage", "--tenant", "acme", "--period", "day",
              "--start", "2026-10-02")  # fmt: skip
    assert as_json(js)["totals"]["amount_micros"] == 93600
    params = tenant.calls.last.request.url.params
    assert params["period"] == "day" and params["start"].startswith("2026-10-02T00:00:00")
    bad = mapi("--url", URL, "usage", "--period", "year")
    assert bad.exit_code == 2


def test_runs_show_cost(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    from mapi_spec.cost import RunCost

    run = example(Run, state="succeeded", finished_at="2026-10-15T10:00:00Z")
    run["cost"] = RunCost.model_json_schema()["examples"][1]
    api.get(f"{V1}/runs/{run['id']}").respond(200, json=run)
    res = mapi("--url", URL, "runs", "get", run["id"])
    assert res.exit_code == 0, res.output
    assert "0.031200 USD" in res.stdout and "1,200 vCPU-s" in res.stdout
    assert "cloudrun-list-2026-10" in res.stdout and "estimate" in res.stdout
    estimated = {**run, "cost": {"estimated": True, "amount_micros": 30000, "currency": "USD"}}
    api.get(f"{V1}/runs").respond(200, json=page([estimated]))
    ls = mapi("--url", URL, "runs", "ls")
    assert "~0.030000 USD" in ls.stdout
    legacy = {**run, "cost": {"estimated": True}}  # a v0.2 server: not metered
    api.get(f"{V1}/runs/{run['id']}").respond(200, json=legacy)
    assert "cost" not in mapi("--url", URL, "runs", "get", run["id"]).stdout


def test_quota_errors_say_when_to_retry(
    mapi: Invoke, api: respx.MockRouter, as_json: AsJson
) -> None:
    api.post(f"{V1}/tokens").respond(
        429,
        json=problem(ErrorCode.QUOTA_EXCEEDED, "tenant acme used its 86400 vCPU-s today"),
        headers={"Retry-After": "5400"},
    )
    res = mapi("--url", URL, "tokens", "create", "x", "-s", "models:read")
    assert res.exit_code == 1
    assert "QUOTA_EXCEEDED" in res.stderr and "retry after 1h 30m" in res.stderr
    assert "mapi usage" in res.stderr
    js = mapi("--url", URL, "--json", "tokens", "create", "x", "-s", "models:read")
    doc = as_json(js)
    assert doc["error"]["code"] == "QUOTA_EXCEEDED" and doc["retry_after_seconds"] == 5400


# ------------------------------------------------------------------------------- admin


def test_admin_kill_switches(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    current = example(KillSwitches, untrusted_execution_enabled=True)
    api.get(f"{V1}/admin/kill-switches").respond(200, json=current)
    got = mapi("--url", URL, "admin", "kill-switches", "get")
    assert got.exit_code == 0 and "running" in got.stdout and "500.000000 USD" in got.stdout
    put = api.put(f"{V1}/admin/kill-switches").respond(200, json=example(KillSwitches))
    args = ("--url", URL, "admin", "kill-switches", "set", "--untrusted-code", "off")
    declined = mapi(*args, "--reason", "incident 42", input="n\n")
    assert declined.exit_code == 1 and not put.called
    assert "Pause ALL runs of uploaded code" in declined.stderr
    assert "untrusted_execution_enabled" in declined.stderr  # the before/after table
    ok = mapi(*args, "--reason", "incident 42", input="y\n")
    assert ok.exit_code == 0, ok.output
    assert json.loads(put.calls.last.request.content) == {
        "untrusted_execution_enabled": False,
        "clear_spend_cap": False,
        "reason": "incident 42",
    }
    assert "PAUSED" in ok.stdout
    no_reason = mapi(*args)
    assert no_reason.exit_code == 2  # --reason is required
    cap = mapi("--url", URL, "admin", "kill-switches", "set", "--spend-cap", "750.5",
               "--reason", "beta cap", "--yes")  # fmt: skip
    assert cap.exit_code == 0, cap.output
    assert json.loads(put.calls.last.request.content)["global_spend_cap_micros"] == 750_500_000
    same = mapi("--url", URL, "admin", "kill-switches", "set", "--untrusted-code", "on",
                "--reason", "noop", "--yes")  # fmt: skip
    assert same.exit_code == 1 and "nothing to change" in same.stderr


def test_admin_quotas(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/admin/quotas").respond(200, json=page([example(QuotaPolicy)]))
    ls = mapi("--url", URL, "admin", "quotas", "ls", "--scope", "tenant")
    assert ls.exit_code == 0 and "acme" in ls.stdout
    put = api.put(f"{V1}/admin/quotas").respond(200, json=example(QuotaPolicy))
    res = mapi("--url", URL, "admin", "quotas", "set", "tenant", "acme", "--concurrent-runs",
               "40", "--storage", "100GiB", "--max-resource-class", "M", "--note", "beta",
               input="y\n")  # fmt: skip
    assert res.exit_code == 0, res.output
    assert json.loads(put.calls.last.request.content) == {
        "scope": "tenant",
        "subject_id": "acme",
        "limits": {"concurrent_runs": 40, "storage_bytes": 100 * 2**30, "max_resource_class": "M"},
        "note": "beta",
    }
    assert "now:" in res.stderr and "after:" in res.stderr
    bad = mapi("--url", URL, "admin", "quotas", "set", "tenant", "acme", "--storage", "lots",
               "--yes")  # fmt: skip
    assert bad.exit_code == 1 and "not a size" in bad.stderr
    rm = api.delete(f"{V1}/admin/quotas/token/tok_1").respond(204)
    assert mapi("--url", URL, "admin", "quotas", "rm", "token", "tok_1", "--yes").exit_code == 0
    assert rm.called


def test_admin_tenants(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/admin/tenants").respond(200, json=page([example(Tenant)]))
    assert "Acme Retail" in mapi("--url", URL, "admin", "tenants", "ls").stdout
    create = api.post(f"{V1}/admin/tenants").respond(201, json=example(Tenant))
    res = mapi("--url", URL, "admin", "tenants", "create", "acme", "--name", "Acme Retail",
               "--residency", "eu", "--admin-email", "it@acme.example", "--yes")  # fmt: skip
    assert res.exit_code == 0, res.output
    assert json.loads(create.calls.last.request.content)["initial_admin_email"] == (
        "it@acme.example"
    )
    api.get(f"{V1}/admin/tenants/acme").respond(200, json=example(Tenant))
    missing = mapi("--url", URL, "admin", "tenants", "update", "acme", "--status", "suspended")
    assert missing.exit_code == 1 and "status_reason" in missing.stderr
    patch = api.patch(f"{V1}/admin/tenants/acme").respond(
        200, json=example(Tenant, status="suspended", status_reason="overdue")
    )
    res = mapi("--url", URL, "admin", "tenants", "update", "acme", "--status", "suspended",
               "--reason", "overdue", input="y\n")  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "only be able to read" in res.stderr
    assert json.loads(patch.calls.last.request.content) == {
        "status": "suspended",
        "status_reason": "overdue",
    }


def test_admin_executors_and_audit(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/admin/executors").respond(200, json=page([example(ExecutorHealth)]))
    ls = mapi("--url", URL, "admin", "executors", "ls")
    assert ls.exit_code == 0 and "cloudrun-jobs" in ls.stdout
    api.get(f"{V1}/admin/executors/cloudrun-jobs").respond(200, json=example(ExecutorHealth))
    got = mapi("--url", URL, "admin", "executors", "get", "cloudrun-jobs")
    assert got.exit_code == 0 and "MICROVM" in got.stdout
    audit = api.get(f"{V1}/admin/audit").respond(200, json=page([example(AuditEvent)]))
    res = mapi("--url", URL, "admin", "audit", "--tenant", "acme", "--action", "token.create",
               "--since", "2026-10-01")  # fmt: skip
    assert res.exit_code == 0 and "token.create" in res.stdout
    params = audit.calls.last.request.url.params
    assert params["tenant_id"] == "acme" and params["since"].startswith("2026-10-01T00:00:00")
    forbidden = api.get(f"{V1}/admin/kill-switches").respond(
        403, json=problem(ErrorCode.FORBIDDEN, "operators only")
    )
    res = mapi("--url", URL, "admin", "kill-switches", "get")
    assert res.exit_code == 1 and "mapi whoami" in res.stderr and forbidden.called


# ------------------------------------------------------------------------------ config


def test_config_set_unset_and_show(mapi: Invoke, isolated_env: Path) -> None:
    assert mapi("--profile", "dev", "config", "set", "url", "https://dev.test/").exit_code == 0
    assert mapi("--profile", "dev", "config", "set", "project", "acme/growth").exit_code == 0
    assert mapi("--profile", "dev", "config", "set", "timeout", "12").exit_code == 0
    assert config_of(isolated_env)["profiles"]["dev"] == {
        "url": "https://dev.test",
        "project": "acme/growth",
        "timeout": 12.0,
    }
    refused = mapi("--profile", "dev", "config", "set", "token", TOKEN)
    assert refused.exit_code == 1 and "secret" in refused.stderr
    assert_no_secret(refused)
    assert mapi("config", "set", "colour", "red").exit_code == 1
    assert mapi("config", "set", "project", "BAD SLUG").exit_code == 1
    assert mapi("--profile", "dev", "config", "unset", "timeout").exit_code == 0
    assert "timeout" not in config_of(isolated_env)["profiles"]["dev"]
    write_config(isolated_env, f'[profiles.dev]\nurl = "https://dev.test"\ntoken = "{TOKEN}"\n')
    show = mapi("--profile", "dev", "config", "show")
    assert show.exit_code == 0 and f"token {TOKEN[:14]}…" in show.stdout
    assert_no_secret(show)
    js = mapi("--json", "--profile", "dev", "config", "show")
    assert json.loads(js.stdout)["token"] == "set"
    assert_no_secret(js)
