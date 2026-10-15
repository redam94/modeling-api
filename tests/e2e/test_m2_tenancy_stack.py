"""The M2 multi-tenant core, through the *running* stack (``make dev``, then ``make e2e``).

Two users in two tenants, each signed in through the stack's development identity provider
(``MAPI_PROFILE=dev``): the platform operator (``admin@dev.local``) creates the tenants and
invites one user as each tenant's admin; each admin creates a project, uploads data (signed
PUT, sandboxed ingest), fits the corpus' scikit-learn store-demand model and scores it. On
that state the suite proves, from outside the processes:

* sign-in: the BFF cookie flow (authorization code + PKCE through the IdP, CSRF on cookie
  mutations, logout), ID-token bearers, API tokens and their revocation;
* isolation: every cross-tenant read or write (datasets, runs, SSE, logs, specs, models,
  artifacts, ``ui/`` views, plots, score, tokens, audit, usage) is refused with 404/403 at
  the API, and Postgres row-level security hides the other tenant's rows even from a
  connection that bypasses the API (fail-closed without a tenant);
* tokens: a token without ``runs:create`` cannot fit or score, and is bound to its project;
* quotas: an exhausted tenant or token budget answers ``QUOTA_EXCEEDED`` (429);
* kill switch: with the global spend cap reached, a queued run of a capped tenant is held at
  dispatch (and new ones are refused) while an ``internal`` tenant keeps running;
* cost: every finished run has a metered cost (ledger entry, ``Run.cost``, usage report);
* audit: the actions above are in the tenant's and the operator's audit logs.

Tenants run on the stack's ``local-process`` executor, which only the ``internal`` (and
``self-hosted-trusted``) tiers may use (plan §8.4: shared tiers need gVisor or a microVM),
so both tenants are created ``internal``; the kill-switch test turns one ``verified`` for
the time the switch is tested.

The kill-switch test pauses the stack's worker (``SIGSTOP``, from ``scripts/dev.py``'s state
file) for a moment so a run is deterministically queued before the switch flips; it is
skipped when the stack was not started by ``scripts/dev.py``.

Accounts: ``MAPI_E2E_ACCOUNTS`` (default ``analyst@dev.local,viewer@dev.local``, two of the
dev IdP's default accounts; they also belong to the ``default`` tenant, which gives the
tenant switcher something to switch between). Tenants are named per session
(``acme-<token>``, ``globex-<token>``) so the suite can run repeatedly against one stack.
"""

from __future__ import annotations

import json
import os
import signal
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from mapi_client import Client
from mapi_client.oidc import dev_idp_login
from mapi_conformance.runner import Fixture, load_fixture
from mapi_conformance.stack import StackRefs, stack_request, stack_spec_yaml
from mapi_spec.api import Dataset, ModelVersion, Run
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.localdev import LocalDev
from mapi_spec.tenancy import PROJECT_HEADER

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[2]
OPERATOR: Final = "admin@dev.local"
FIXTURE: Final = "sklearn_store_demand"
SIZE: Final = "tiny"
PROJECT: Final = "growth"
SCORE_CASE: Final = "forecast_with_new_store"
RUN_TIMEOUT_S: Final = 900.0
UPLOAD: Final = "upl_01m50e1xhjq97g5sjmnqp1y19d"
"""A well-formed upload id (the version request must reach the dataset lookup)."""
ZARR: Final = {"format": "zarr_zip"}
DENIED: Final = frozenset({403, 404})
"""Cross-tenant answers: the resource is unknown to the caller (404) or the action is not
theirs (403). Never 2xx, never 5xx."""


# ================================================================================== stack


@dataclass(frozen=True)
class Stack:
    local: LocalDev
    data_dir: Path

    @property
    def api(self) -> str:
        return self.local.api_url.rstrip("/")

    @property
    def database_url(self) -> str:
        if self.local.database_url:
            return self.local.database_url
        return f"postgresql://postgres:@/mapi?host={self.data_dir / 'pg'}"

    def worker_pid(self) -> int | None:
        """The ``mapi-server worker`` pid of a ``scripts/dev.py`` stack (``None`` otherwise)."""
        state = self.data_dir / "dev" / "supervisor.json"
        try:
            pid = json.loads(state.read_text(encoding="utf-8"))["services"]["worker"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return None
        return int(pid)


@pytest.fixture(scope="module")
def stack() -> Stack:
    local = LocalDev.from_environ(os.environ)
    data_dir = Path(local.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    st = Stack(local=local, data_dir=data_dir)
    try:
        r = httpx.get(f"{st.api}/dev/idp/.well-known/openid-configuration", timeout=10)
    except httpx.TransportError as exc:
        pytest.fail(f"the API is not reachable at {st.api} ({exc}); start it with `make dev`")
    if r.status_code != 200:
        pytest.skip("the M2 tenancy suite needs a stack in the dev profile (make dev)")
    return st


@pytest.fixture(scope="module")
def session_token() -> str:
    return uuid.uuid4().hex[:6]


def login(stack: Stack, email: str) -> str:
    """An ID token for ``email`` from the stack's development IdP (code + PKCE, no browser)."""
    return dev_idp_login(stack.api, email)


@pytest.fixture(scope="module")
def operator(stack: Stack) -> Iterator[Client]:
    with Client(stack.api, token=login(stack, OPERATOR)) as client:
        assert client.me().operator, f"{OPERATOR} must be a platform operator"
        yield client


@pytest.fixture(scope="module")
def raw(stack: Stack) -> Iterator[httpx.Client]:
    """Plain HTTP for status-code assertions (no client-side retries or parsing)."""
    with httpx.Client(base_url=stack.api, timeout=60, follow_redirects=False) as client:
        yield client


# ================================================================================ tenants


@dataclass
class Side:
    """One tenant, its admin and everything they create."""

    tenant: str
    email: str
    seed: int
    id_token: str
    client: Client
    project: str = ""
    refs: StackRefs = field(default_factory=StackRefs)
    datasets: list[Dataset] = field(default_factory=list)
    spec_id: str = ""
    fit: Run | None = None
    version: ModelVersion | None = None
    score: Run | None = None

    def headers(self, project: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.id_token}",
            PROJECT_HEADER: project or self.project,
        }

    @property
    def mv(self) -> ModelVersion:
        assert self.version is not None, "the fit did not register a model version"
        return self.version


def _accounts() -> tuple[str, str]:
    raw_value = os.environ.get("MAPI_E2E_ACCOUNTS", "analyst@dev.local,viewer@dev.local")
    accounts = [a.strip().lower() for a in raw_value.split(",") if a.strip()]
    if len(accounts) != 2 or OPERATOR in accounts or accounts[0] == accounts[1]:
        pytest.fail("MAPI_E2E_ACCOUNTS must name two distinct non-operator dev IdP accounts")
    return accounts[0], accounts[1]


@pytest.fixture(scope="module")
def tenants(stack: Stack, operator: Client, session_token: str) -> Iterator[tuple[Side, Side]]:
    """``acme-*`` and ``globex-*``: created by the operator with an invited admin each, who
    signs in (claiming the invitation) and creates project ``growth``."""
    sides: list[Side] = []
    for (name, title), email, seed in zip(
        (("acme", "Acme Retail"), ("globex", "Globex Corp")), _accounts(), (0, 1), strict=True
    ):
        tenant = f"{name}-{session_token}"
        created = operator.admin.create_tenant(
            {
                "id": tenant,
                "name": f"{title} ({session_token})",
                "trust_tier": "internal",
                "initial_admin_email": email,
            }
        )
        assert created.id == tenant and created.trust_tier.value == "internal"
        token = login(stack, email)
        client = Client(stack.api, token=token)
        me = client.me()
        mine = {t.tenant.id: t.role.value if t.role else None for t in me.tenants}
        assert mine.get(tenant) == "admin", f"{email} did not claim the admin invitation: {mine}"
        project = client.tenants.create_project(tenant, PROJECT, f"{title} growth")
        client.use(f"{tenant}/{project.id}")
        side = Side(tenant=tenant, email=email, seed=seed, id_token=token, client=client)
        side.project = f"{tenant}/{project.id}"
        sides.append(side)
    try:
        yield sides[0], sides[1]
    finally:
        for side in sides:
            side.client.close()


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    return load_fixture(FIXTURE)


def _upload(side: Side, fx: Fixture, session_token: str) -> None:
    """The fixture's data and score payload, under the *same* names in both tenants (the
    ``dataset://growth/...`` refs are identical strings; each resolves in its own tenant)."""
    data, tables = fx.generate(side.seed, SIZE)
    assert not tables, "the store-demand fixture has no side tables"
    spec = fx.spec(SIZE)
    schema = fx.raw_schema(data)
    name = f"store-sku-weekly-{session_token}"
    version = side.client.datasets.upload(
        data.assign_attrs({DATASET_SCHEMA_ATTR: schema.model_dump_json()}),
        dataset=name,
        timeout=RUN_TIMEOUT_S,
    )
    assert version.status.value == "ready", version
    assert version.tenant_id == side.tenant
    side.refs.data = version.ref
    case = next(c for c in fx.cases if c.name == SCORE_CASE)
    payload = fx.payload(case, data, spec, schema, side.seed, SIZE)
    assert payload is not None
    future = side.client.datasets.upload(
        payload, dataset=f"store-future-{session_token}", timeout=RUN_TIMEOUT_S
    )
    assert future.status.value == "ready", future
    side.refs.payloads[case.payload] = future.ref
    side.datasets = [side.client.datasets.get(version.dataset_id)]


def _spec_text(side: Side, fx: Fixture, session_token: str) -> str:
    doc = json.loads(stack_spec_yaml(fx, SIZE, side.refs, name=f"store-demand-{session_token}"))
    doc["metadata"]["project"] = PROJECT
    return json.dumps(doc, indent=1)


@pytest.fixture(scope="module")
def fitted(tenants: tuple[Side, Side], fixture: Fixture, session_token: str) -> tuple[Side, Side]:
    """Both tenants upload, validate, create and fit (concurrently), then score once."""
    for side in tenants:
        _upload(side, fixture, session_token)
        text = _spec_text(side, fixture, session_token)
        verdict = side.client.specs.validate(text)
        assert verdict.valid, [i.model_dump() for i in verdict.issues]
        doc = side.client.specs.create(text, message="m2 e2e")
        side.spec_id = doc.id
        side.fit = side.client.runs.fit(doc.id, doc.latest_revision, labels={"suite": "m2-e2e"})
    for side in tenants:
        assert side.fit is not None
        final = side.client.runs.wait(side.fit.id, timeout=RUN_TIMEOUT_S)
        assert final.state.value == "succeeded", final.problem
        side.fit = final
        models = [m for m in side.client.models.list() if m.name == f"store-demand-{session_token}"]
        assert len(models) == 1, models
        side.version = side.client.models.version(models[0].id, models[0].latest_version or 1)
    spec = fixture.spec(SIZE)
    case = next(c for c in fixture.cases if c.name == SCORE_CASE)
    for side in tenants:
        request = stack_request(case, spec, side.refs, side.mv.ref)
        result = side.client.models.score(
            side.mv.model_id, side.mv.version, request, timeout=RUN_TIMEOUT_S, download=False
        )
        assert result.run.state.value == "succeeded", result.run.problem
        side.score = result.run
    return tenants


# ============================================================================ sign-in


def test_bff_cookie_sign_in_csrf_and_logout(stack: Stack, tenants: tuple[Side, Side]) -> None:
    """The browser flow: /auth/login → IdP → /auth/callback → session + CSRF cookies."""
    acme, _ = tenants
    with httpx.Client(base_url=stack.api, timeout=30, follow_redirects=False) as browser:
        r = browser.get("/auth/login", params={"return_to": "/runs"})
        assert r.status_code == 302
        authorize = r.headers["location"]
        assert "/dev/idp/authorize" in authorize and "code_challenge_method=S256" in authorize
        page = browser.get(authorize)  # the IdP's account chooser
        assert page.status_code == 200 and acme.email in page.text
        r = browser.get(authorize + "&" + httpx.QueryParams({"login_hint": acme.email}).__str__())
        assert r.status_code == 302
        callback = urlsplit(r.headers["location"])
        assert callback.path == "/auth/callback"
        assert set(parse_qs(callback.query)) == {"code", "state"}
        r = browser.get(f"{callback.path}?{callback.query}")
        assert r.status_code == 302, r.text
        assert r.headers["location"] == stack.local.web_origin.rstrip("/") + "/runs"
        session = browser.get("/auth/session").json()
        assert session["authenticated"] is True
        me = browser.get("/v1/me").json()
        assert me["principal"]["email"] == acme.email and me["auth"] == "session"
        assert acme.tenant in {t["tenant"]["id"] for t in me["tenants"]}
        # a replayed callback (one-time login transaction) is refused
        assert browser.get(f"{callback.path}?{callback.query}").status_code == 401
        # cookie-authenticated mutations need the CSRF token and a same-origin request
        body = {"name": "csrf probe", "scopes": ["models:read"], "expires_in_days": 1}
        headers = {PROJECT_HEADER: acme.project}
        r = browser.post("/v1/tokens", json=body, headers=headers)
        assert r.status_code == 403 and "X-CSRF-Token" in r.json()["detail"]
        csrf = next(v for k, v in browser.cookies.items() if "csrf" in k)
        evil = {**headers, "X-CSRF-Token": csrf, "Origin": "https://evil.example"}
        assert browser.post("/v1/tokens", json=body, headers=evil).status_code == 403
        r = browser.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 204
        browser.cookies.clear()
        assert browser.get("/v1/me").status_code == 401
        assert browser.get("/auth/session").json()["authenticated"] is False


def test_bearer_flows_and_unauthenticated_requests(
    stack: Stack, raw: httpx.Client, tenants: tuple[Side, Side]
) -> None:
    acme, globex = tenants
    # no credentials, a malformed bearer and a forged API token: 401 on project routes
    assert raw.get("/v1/datasets", headers={PROJECT_HEADER: acme.project}).status_code == 401
    bad = {"Authorization": "Basic Zm9vOmJhcg==", PROJECT_HEADER: acme.project}
    assert raw.get("/v1/datasets", headers=bad).status_code == 401
    forged = {"Authorization": "Bearer mapi_xxxxxxxx_" + "a" * 32, PROJECT_HEADER: acme.project}
    assert raw.get("/v1/datasets", headers=forged).status_code == 401
    # an ID token from the dev IdP is a bearer for the CLI and scripts
    me = raw.get("/v1/me", headers={"Authorization": f"Bearer {acme.id_token}"}).json()
    assert me["principal"]["email"] == acme.email
    assert {t["tenant"]["id"] for t in me["tenants"]} >= {acme.tenant}
    assert globex.tenant not in {t["tenant"]["id"] for t in me["tenants"]}
    # an API token: minted once, works, is bound to its project, and dies on revocation
    created = acme.client.tokens.create("e2e-reader", ["models:read"], expires_in_days=1)
    auth = {"Authorization": f"Bearer {created.secret}"}
    me = raw.get("/v1/me", headers=auth).json()
    assert me["auth"] == "token" and me["token"]["id"] == created.token.id
    assert raw.get("/v1/models", headers=auth).status_code == 200
    assert raw.get("/v1/models", headers={**auth, PROJECT_HEADER: globex.project}).status_code in (
        DENIED
    )
    acme.client.tokens.revoke(created.token.id)
    assert raw.get("/v1/models", headers=auth).status_code == 401


# ======================================================================= fit and cost


def test_each_tenant_fits_and_scores_on_its_own_data(fitted: tuple[Side, Side]) -> None:
    acme, globex = fitted
    # the same dataset ref string resolves to each tenant's own data
    assert acme.refs.data == globex.refs.data
    assert acme.mv.tenant_id == acme.tenant and globex.mv.tenant_id == globex.tenant
    assert acme.mv.lineage.dataset_sha and globex.mv.lineage.dataset_sha
    assert acme.mv.lineage.dataset_sha != globex.mv.lineage.dataset_sha
    for side in fitted:
        assert side.mv.manifest is not None and side.mv.artifacts
        assert {"rmse", "coverage_80"} <= set(side.mv.metrics)
        assert side.mv.lineage.executor == "local-process"


def test_per_run_cost_is_metered_and_reported(
    stack: Stack, fitted: tuple[Side, Side], operator: Client
) -> None:
    for side in fitted:
        for run_id in (side.fit.id if side.fit else "", side.score.id if side.score else ""):
            run = side.client.runs.get(run_id)
            cost = run.cost
            assert cost is not None, run_id
            assert cost.estimated is False and cost.attempts_billed == 1, cost
            assert cost.amount_micros is not None and cost.amount_micros > 0, cost
            assert cost.currency == "USD" and cost.price_version, cost
            assert cost.vcpu_seconds and cost.vcpu_seconds > 0 and cost.gib_seconds, cost
            assert cost.estimate_micros is not None and cost.estimate_micros > 0, cost
            assert cost.usd == pytest.approx(cost.amount_micros / 1e6)
        # the ledger rows behind it, one actual entry per metered attempt
        with _db(stack) as conn:
            rows = conn.execute(
                "SELECT run_id, tenant_id, kind, amount_micros FROM cost_ledger "
                "WHERE run_id = ANY(%s)",
                ([side.fit.id, side.score.id] if side.fit and side.score else [],),
            ).fetchall()
        assert len(rows) == 2 and {r[1] for r in rows} == {side.tenant}, rows
        assert {r[2] for r in rows} == {"actual"}
        # the project's usage report and the tenant view add the same entries up
        usage = side.client.usage.get()
        total = sum(r[3] for r in rows)
        assert usage.totals.amount_micros >= total, usage.totals
        tenant_usage = side.client.usage.tenant(side.tenant)
        assert tenant_usage.totals.amount_micros == usage.totals.amount_micros
    # the operator sees per-tenant cost too
    acme, _ = fitted
    assert operator.usage.tenant(acme.tenant).totals.amount_micros > 0


# ===================================================================== isolation: API


def _score_request(model_ref: str, data_ref: str) -> dict[str, Any]:
    """A valid forecast request body (raw JSON, as a non-Python client sends it)."""
    return {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": model_ref,
        "mode": "forecast",
        "horizon": {"dim": "time", "periods": 4},
        "data": data_ref,
    }


def _probe(raw: httpx.Client, headers: dict[str, str], method: str, path: str, **kw: Any) -> int:
    return raw.request(method, path, headers=headers, **kw).status_code


def test_cross_tenant_access_is_denied_at_the_api(
    raw: httpx.Client, fitted: tuple[Side, Side]
) -> None:
    acme, globex = fitted
    assert acme.fit and acme.score and globex.fit
    ds = acme.datasets[0]
    mv = acme.mv
    artifact = next(a.name for a in mv.artifacts if not a.name.startswith("ui/"))
    ui = next((a.name for a in mv.artifacts if a.name.startswith("ui/")), "ui/summaries.json")
    plot_ids = [p.id for p in acme.client.plots.list(mv.model_id, mv.version)]
    assert plot_ids, "the fit stored no plots"
    a = f"/v1/models/{mv.model_id}/versions/{mv.version}"
    reads = [
        f"/v1/datasets/{ds.id}",
        f"/v1/datasets/{ds.id}/versions",
        f"/v1/datasets/{ds.id}/versions/1",
        f"/v1/datasets/{ds.id}/versions/1/preview",
        f"/v1/datasets/{ds.id}/versions/1/artifacts/data.zarr.zip",
        f"/v1/specs/{acme.spec_id}",
        f"/v1/specs/{acme.spec_id}/revisions/1",
        f"/v1/runs/{acme.fit.id}",
        f"/v1/runs/{acme.fit.id}/logs",
        f"/v1/runs/{acme.fit.id}/plots",
        f"/v1/runs/{acme.score.id}",
        f"/v1/models/{mv.model_id}",
        f"/v1/models/{mv.model_id}/versions",
        a,
        f"{a}/artifacts/{artifact}",
        f"/{a.lstrip('/')}/ui/{ui.removeprefix('ui/')}",
        f"{a}/plots",
        f"{a}/plots/{plot_ids[0]}",
    ]
    own = globex.headers()
    for path in reads:
        status = _probe(raw, own, "GET", path)
        assert status in DENIED, f"GET {path} as {globex.email} answered {status}"
        # naming the other tenant's project explicitly is refused before any handler runs
        status = _probe(raw, globex.headers(acme.project), "GET", path)
        assert status in DENIED, f"GET {path} in {acme.project} answered {status}"
    # the SSE stream: refused before a single frame is sent
    with raw.stream(
        "GET", f"/v1/runs/{acme.fit.id}/events", headers={**own, "Accept": "text/event-stream"}
    ) as r:
        assert r.status_code in DENIED
    # valid bodies, so a refusal can only come from the lookup in the caller's tenant
    own_spec = globex.client.specs.revision(globex.spec_id, 1).yaml
    score = {"request": _score_request(mv.ref, globex.refs.payloads["future"])}
    writes = [
        ("POST", f"/v1/runs/{acme.fit.id}:cancel", None),
        ("POST", f"/v1/runs/{acme.fit.id}:retry", {"reason": "cross-tenant probe"}),
        ("POST", f"/v1/runs/{acme.fit.id}:resume", None),
        ("POST", f"/v1/specs/{acme.spec_id}/revisions", {"yaml": own_spec, "parent_revision": 1}),
        ("POST", f"/v1/specs/{acme.spec_id}/revisions/1:dry-run", None),
        ("POST", f"{a}:score", score),
        ("POST", f"{a}:evaluate", {}),
        ("POST", "/v1/runs", {"kind": "fit", "spec_id": acme.spec_id, "revision": 1}),
        ("POST", f"/v1/datasets/{ds.id}/uploads", {"filename": "x.zarr.zip", "size_bytes": 1}),
        ("POST", f"/v1/datasets/{ds.id}/versions", {"upload_id": UPLOAD, "mapping": ZARR}),
    ]
    for method, path, body in writes:
        status = _probe(raw, own, method, path, json=body)
        assert status in DENIED, f"{method} {path} as {globex.email} answered {status}"
    # tenant-level resources of the other tenant
    for path in (
        f"/v1/tenants/{acme.tenant}",
        f"/v1/tenants/{acme.tenant}/projects",
        f"/v1/tenants/{acme.tenant}/members",
        f"/v1/tenants/{acme.tenant}/audit",
        f"/v1/tenants/{acme.tenant}/usage",
    ):
        status = _probe(raw, {"Authorization": f"Bearer {globex.id_token}"}, "GET", path)
        assert status in DENIED, f"GET {path} as {globex.email} answered {status}"
    # operator-only routes
    for path in ("/v1/admin/tenants", "/v1/admin/kill-switches", "/v1/admin/audit"):
        status = _probe(raw, {"Authorization": f"Bearer {globex.id_token}"}, "GET", path)
        assert status == 403, f"GET {path} as a tenant admin answered {status}"
    # lists never show the other tenant's rows, even with the same names
    names = {d.id for d in globex.client.datasets.list()}
    assert ds.id not in names
    assert acme.fit.id not in {r.id for r in globex.client.runs.list()}
    assert mv.model_id not in {m.id for m in globex.client.models.list()}
    # a score request naming the other tenant's model ref resolves in the caller's tenant
    with pytest.raises(MapiError) as err:
        globex.client.models.score(
            mv.model_id,
            mv.version,
            _score_request(mv.ref, globex.refs.payloads["future"]),
            timeout=60,
        )
    assert err.value.code in (ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN)


def test_a_signed_artifact_link_is_minted_only_for_the_owner(
    raw: httpx.Client, fitted: tuple[Side, Side]
) -> None:
    """Signed URLs are object-scoped and short-lived; the other tenant cannot mint one."""
    acme, globex = fitted
    mv = acme.mv
    name = next(a.name for a in mv.artifacts if not a.name.startswith("ui/"))
    link = acme.client.models.artifact(mv.model_id, mv.version, name)
    assert f"/t/{acme.tenant}/" in link.url or acme.tenant in link.url
    path = f"/v1/models/{mv.model_id}/versions/{mv.version}/artifacts/{name}"
    assert raw.get(path, headers=globex.headers()).status_code in DENIED


# ===================================================================== isolation: RLS


@contextmanager
def _db(stack: Stack) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(stack.database_url, autocommit=True) as conn:
        yield conn


def test_row_level_security_hides_the_other_tenant(stack: Stack, fitted: tuple[Side, Side]) -> None:
    """Straight to Postgres as the request-path role: RLS, not the API, is the last line."""
    acme, globex = fitted
    assert acme.fit and globex.fit
    tables = ("runs", "datasets", "dataset_versions", "specs", "models", "model_versions")
    with _db(stack) as conn:
        app_role = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = 'mapi_app'"
        ).fetchone()
        assert app_role is not None, "the request-path role mapi_app is missing"
        forced = dict(
            conn.execute(
                "SELECT relname, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
                (list(tables),),
            ).fetchall()
        )
        assert forced == dict.fromkeys(tables, True), forced
        for table in tables:
            total = conn.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (globex.tenant,)
            ).fetchone()
            assert total is not None and total[0] > 0, f"{globex.tenant} has no {table} rows"
    with psycopg.connect(stack.database_url) as conn:  # transactions, like the API
        # fresh connection, no tenant: fail closed (current_setting(..., false) raises)
        conn.execute("SET ROLE mapi_app")
        with pytest.raises(psycopg.errors.UndefinedObject):
            conn.execute("SELECT count(*) FROM runs").fetchone()
        conn.rollback()
        for table in tables:
            conn.execute("SET ROLE mapi_app")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (acme.tenant,))
            mine = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            theirs = conn.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (globex.tenant,)
            ).fetchone()
            assert mine is not None and mine[0] > 0, f"{acme.tenant} sees none of its {table}"
            assert theirs == (0,), f"{acme.tenant} sees {theirs} {globex.tenant} {table} rows"
            conn.rollback()
        # by id, and writes: the other tenant's run is neither visible nor updatable
        conn.execute("SET ROLE mapi_app")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (acme.tenant,))
        assert conn.execute("SELECT id FROM runs WHERE id = %s", (globex.fit.id,)).fetchall() == []
        cur = conn.execute("UPDATE runs SET labels = '{}'::jsonb WHERE id = %s", (globex.fit.id,))
        assert cur.rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE runs SET tenant_id = %s WHERE id = %s", (globex.tenant, acme.fit.id)
            )
        conn.rollback()
        # the setting is transaction-local: after commit a pooled connection sees nothing
        conn.execute("SET ROLE mapi_app")
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (acme.tenant,))
        conn.commit()
        conn.execute("SET ROLE mapi_app")
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)
        conn.rollback()


# ======================================================================= tokens/scopes


def test_a_token_without_runs_create_cannot_fit_or_score(
    raw: httpx.Client, fitted: tuple[Side, Side]
) -> None:
    acme, globex = fitted
    reader = acme.client.tokens.create("e2e-models-read", ["models:read"], expires_in_days=1)
    auth = {"Authorization": f"Bearer {reader.secret}"}
    mv = acme.mv
    assert raw.get(f"/v1/models/{mv.model_id}", headers=auth).status_code == 200
    r = raw.post(
        "/v1/runs", headers=auth, json={"kind": "fit", "spec_id": acme.spec_id, "revision": 1}
    )
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", r.text
    assert "create run" in r.json()["detail"]
    score = {"request": _score_request(mv.ref, acme.refs.payloads["future"])}
    r = raw.post(f"/v1/models/{mv.model_id}/versions/{mv.version}:score", headers=auth, json=score)
    assert r.status_code == 403, r.text
    # and a token cannot mint tokens or be pointed at another tenant's project
    r = raw.post("/v1/tokens", headers=auth, json={"name": "x", "scopes": ["models:read"]})
    assert r.status_code == 403
    r = raw.get(f"/v1/models/{mv.model_id}", headers={**auth, PROJECT_HEADER: globex.project})
    assert r.status_code in DENIED
    # the same request with a runs:create token is admitted (and the fit succeeds)
    runner = acme.client.tokens.create("e2e-runner", ["runs:create"], expires_in_days=1)
    with Client(acme.client.config.base_url, token=runner.secret) as as_token:
        run = as_token.runs.fit(acme.spec_id, 1, labels={"suite": "m2-e2e", "via": "token"})
        final = as_token.runs.wait(run.id, timeout=RUN_TIMEOUT_S)
        assert final.state.value == "succeeded", final.problem
    # the audit log attributes the run to the token
    created = [
        e
        for e in acme.client.tenants.audit(acme.tenant, action="run.create")
        if e.resource_id == run.id
    ]
    assert len(created) == 1 and created[0].token_id == runner.token.id, created
    for t in (reader, runner):
        acme.client.tokens.revoke(t.token.id)


# =============================================================================== quotas


def test_exhausted_quotas_answer_quota_exceeded(
    raw: httpx.Client, fitted: tuple[Side, Side], operator: Client
) -> None:
    acme, globex = fitted
    body = {"kind": "fit", "spec_id": acme.spec_id, "revision": 1}
    # tenant level: the operator caps acme's daily compute below what it has used
    operator.admin.set_quota(
        "tenant", acme.tenant, {"vcpu_seconds_per_day": 1}, note="m2 e2e: exhausted"
    )
    try:
        r = raw.post("/v1/runs", headers=acme.headers(), json=body)
        assert r.status_code == 429, r.text
        problem = r.json()
        assert problem["code"] == "QUOTA_EXCEEDED", problem
        assert acme.tenant in problem["detail"] or "tenant" in problem["detail"]
        # the other tenant is unaffected (admitted; cancelled straight away)
        other = raw.post(
            "/v1/runs",
            headers=globex.headers(),
            json={"kind": "fit", "spec_id": globex.spec_id, "revision": 1},
        )
        assert other.status_code in (200, 201, 202), other.text
        globex.client.runs.cancel(other.json()["id"])
        globex.client.runs.wait(other.json()["id"], timeout=RUN_TIMEOUT_S)
    finally:
        operator.admin.delete_quota("tenant", acme.tenant)
    # token level: a token with a tiny vCPU-second budget
    small = acme.client.tokens.create(
        "e2e-budget", ["runs:create"], expires_in_days=1, vcpu_seconds_budget=1.0
    )
    try:
        r = raw.post("/v1/runs", headers={"Authorization": f"Bearer {small.secret}"}, json=body)
        assert r.status_code == 429 and r.json()["code"] == "QUOTA_EXCEEDED", r.text
        assert "token" in r.json()["detail"]
    finally:
        acme.client.tokens.revoke(small.token.id)


# ========================================================================= kill switch


def _held(side: Side, run_id: str, seconds: float) -> None:
    """The run stays ``queued`` for ``seconds`` (several dispatcher polls)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = side.client.runs.get(run_id).state.value
        assert state == "queued", f"run {run_id} left the queue ({state}) under the kill switch"
        time.sleep(0.5)


def test_the_spend_cap_kill_switch_holds_dispatch(
    stack: Stack, raw: httpx.Client, fitted: tuple[Side, Side], operator: Client
) -> None:
    acme, globex = fitted
    pid = stack.worker_pid()
    if pid is None:
        pytest.skip("needs the worker pid of a scripts/dev.py stack to pause the dispatcher")
    body = {"kind": "fit", "spec_id": acme.spec_id, "revision": 1}
    queued: str | None = None
    os.kill(pid, signal.SIGSTOP)  # the dispatcher cannot lease while the run is created
    try:
        r = raw.post("/v1/runs", headers=acme.headers(), json=body)
        assert r.status_code in (200, 201, 202), r.text
        queued = r.json()["id"]
        assert r.json()["state"] == "queued"
        # the operator makes acme a shared-tier tenant and caps platform spend below today's
        operator.admin.update_tenant(acme.tenant, {"trust_tier": "verified"})
        switches = operator.admin.set_kill_switches(
            {"global_spend_cap_micros": 1, "reason": "m2 e2e: spend cap drill"}
        )
        assert switches.global_spend_cap_micros == 1
    finally:
        os.kill(pid, signal.SIGCONT)
    try:
        # dispatch: held (were it leased, placement would fail it at once: verified tenants
        # need gVisor, and the local stack only has local-process)
        _held(acme, queued, seconds=6.0)
        # admission: new runs of the capped tenant are refused
        r = raw.post("/v1/runs", headers=acme.headers(), json=body)
        assert r.status_code == 429 and r.json()["code"] == "QUOTA_EXCEEDED", r.text
        assert "spend cap" in r.json()["detail"]
        # internal tenants are exempt: globex is admitted and dispatched meanwhile
        other = globex.client.runs.fit(globex.spec_id, 1, labels={"suite": "m2-e2e"})
        final = globex.client.runs.wait(other.id, timeout=RUN_TIMEOUT_S)
        assert final.state.value == "succeeded", final.problem
        assert acme.client.runs.get(queued).state.value == "queued"
        # back to internal (exempt): the held run is leased and finishes
        operator.admin.update_tenant(acme.tenant, {"trust_tier": "internal"})
        done = acme.client.runs.wait(queued, timeout=RUN_TIMEOUT_S)
        assert done.state.value == "succeeded", done.problem
    finally:
        operator.admin.set_kill_switches({"clear_spend_cap": True, "reason": "m2 e2e: done"})
        operator.admin.update_tenant(acme.tenant, {"trust_tier": "internal"})
    assert operator.admin.kill_switches().global_spend_cap_micros is None


# ================================================================================ audit


def test_audit_entries_are_written(
    raw: httpx.Client, fitted: tuple[Side, Side], operator: Client
) -> None:
    acme, globex = fitted
    actions = {e.action.value for e in acme.client.tenants.audit(acme.tenant)}
    assert {"project.create", "run.create", "token.create", "token.revoke"} <= actions, actions
    platform = list(operator.admin.audit())
    by_action: dict[str, list[Any]] = {}
    for e in platform:
        by_action.setdefault(e.action.value, []).append(e)
    created = {e.resource_id for e in by_action.get("tenant.create", [])}
    assert {acme.tenant, globex.tenant} <= created
    assert any(e.resource_id == f"tenant/{acme.tenant}" for e in by_action.get("quota.update", []))
    assert "kill_switch.update" in by_action
    assert "auth.login" in by_action
    # the other tenant's audit log is not readable
    status = raw.get(
        f"/v1/tenants/{acme.tenant}/audit", headers={"Authorization": f"Bearer {globex.id_token}"}
    ).status_code
    assert status in DENIED
    mine = {e.tenant_id for e in globex.client.tenants.audit(globex.tenant)}
    assert mine <= {globex.tenant, None}
