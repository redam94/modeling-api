"""The M3 exit criteria through the *running* stack (``make dev``, then ``make e2e``).

One analyst's whole journey, from outside the processes, against a ``dev``-profile stack:

1. **Sign-up.** ``POST /auth/signup``, the verification e-mail read back from the dev outbox,
   ``POST /auth/verify-email`` (a ``public`` workspace), sign-in through the development IdP.
   The platform operator then makes the workspace ``internal``: the dev stack runs every run
   on the ``local-process`` executor, which shared tiers may not use (cut line A).
2. **Guided data.** The ``geo_mmm`` template; a realistic weekly CSV (five regions, three
   channels, two years) profiled locally like the browser does; role detection; the draft;
   the upload with the draft's mapping; the same data as an MFF long table.
3. **Business priors.** ROI and contribution-share ranges and a lift test, translated by a
   sandboxed ``translate_priors`` run, previewed and written into the spec.
4. **Fit and scorecard.** A tiny fit, whose scorecard and promotion gate the server derives
   exactly as ``mapi_spec.scorecard`` does; then the fit that is promoted.
5. **What-if.** The per-draw response grid answers sliders; "Run exact" (compiled by the
   server) agrees with it at grid points; a forecast and a channel-off counterfactual.
6. **Endpoint.** The gate (``409`` with its checks, then an audited admin override), the
   schema and snippets, scoring with a ``score:<alias>`` token and nothing else.
7. **Webhooks.** A local HTTPS receiver (the dev profile's receiver certificate) gets signed
   ``run.succeeded``, ``model.version.ready``, ``endpoint.promoted`` and ``score.completed``
   deliveries, verified with :func:`mapi_client.webhooks.verify`; a replay arrives again.
8. **Embed.** A saved platform-recipe plot as a read-only embed link, then revoked.
9. **Budget optimisation.** ``POST …:optimize`` on the promoted version: mmm-framework
   planning reallocates the budget within bounds and reports the uplift and the frontier.
10. **GDPR export and deletion.** The export downloads (no secrets inside); with
    ``MAPI_TENANT_DELETION_GRACE_S=0`` the purge removes every tenant row in Postgres and
    every object under ``t/<tenant>/`` in the GCS shim, and the webhook hears
    ``tenant.deletion.completed``.

Timings per step are written to ``$MAPI_DATA_DIR/e2e/m3-guided-timings.json`` (and printed
with ``-s``). The account (``MAPI_E2E_GUIDED_ACCOUNT``, default ``viewer@dev.local``) must be
one the dev IdP offers; every run creates a new workspace.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

import httpx
import pandas as pd
import psycopg
import pytest
import yaml
from mapi_cli.profile import profile_csv
from mapi_client import Client
from mapi_client.oidc import dev_idp_login
from mapi_client.webhooks import verify
from mapi_conformance.fixtures.guided_mmm.data import SIZES, _simulate, truth
from mapi_gcs_shim.client import ShimObjectStore
from mapi_spec.api import ModelVersion, Run
from mapi_spec.calibration import BusinessPriors, apply_prior_translation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.localdev import LocalDev
from mapi_spec.scorecard import Scorecard, promotion_gate
from mapi_spec.tenancy import PROJECT_HEADER
from mapi_spec.trust import TERMS_VERSION
from mapi_spec.views import CounterfactualView, ForecastView, ResponseGridView
from mapi_spec.webhooks import WebhookEnvelope
from mapi_spec.whatif import WhatIfScenario, evaluate_scenario

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[2]
OPERATOR: Final = "admin@dev.local"
TEMPLATE: Final = "geo_mmm"
RUN_TIMEOUT_S: Final = 1200.0
EVENT_TIMEOUT_S: Final = 120.0
SIZE: Final = "full"
"""Two years of five regions and three channels: enough history for the scorecard."""
TINY_FIT: Final = {"tune": 150, "draws": 150, "chains": 2}
ENDPOINT_FIT: Final = {"tune": 1000, "draws": 1000, "chains": 4, "target_accept": 0.95}
"""A fit long enough for the scorecard's convergence checks to pass or warn (not fail)."""
BUSINESS_PRIORS: Final = {
    "channels": {
        "tv": {"roi": [1.5, 4.0], "credibility": 0.8},
        "search": {"contribution_share": [0.06, 0.2], "credibility": 0.8},
    },
    "baseline_share": {"share": [0.6, 0.85], "credibility": 0.8},
}
LIFT_TESTS: Final = [
    {"id": "tv-north", "channel": "tv", "geo": "north", "start": "2024-03-03",
     "end": "2024-03-24", "spend_delta": 4000, "kpi_delta": 5400, "se": 600},
]  # fmt: skip
EVENTS: Final = (
    "run.succeeded",
    "model.version.ready",
    "endpoint.promoted",
    "score.completed",
    "tenant.deletion.completed",
)
COLUMNS: Final = {
    "week": "Week",
    "region": "Region",
    "revenue": "Revenue",
    "tv": "TV Spend",
    "search": "Search Spend",
    "social": "Social Spend",
    "avg_price": "Avg Price",
}
"""How the analyst's spreadsheet names things (the template's questions must find them)."""


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

    @property
    def webhook_tls(self) -> Path:
        """The dev profile's local-receiver certificate (``scripts/dev.py``)."""
        override = os.environ.get("MAPI_E2E_WEBHOOK_TLS_DIR", "").strip()
        return Path(override) if override else self.data_dir / "dev" / "webhook-tls"


@pytest.fixture(scope="module")
def stack() -> Stack:
    local = LocalDev.from_environ(os.environ)
    data_dir = Path(local.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    st = Stack(local=local, data_dir=data_dir)
    try:
        r = httpx.get(f"{st.api}/dev/outbox", timeout=10)
    except httpx.TransportError as exc:
        pytest.fail(f"the API is not reachable at {st.api} ({exc}); start it with `make dev`")
    if r.status_code != 200:
        pytest.skip("the M3 journey needs a dev-profile stack with the outbox mailer (make dev)")
    if not (st.webhook_tls / "cert.pem").exists():
        pytest.fail(
            f"no webhook receiver certificate in {st.webhook_tls}: start the stack with "
            "`make dev` (dev profile), which writes one and trusts its CA"
        )
    return st


# ============================================================================== receiver


@dataclass
class Delivery:
    headers: dict[str, str]
    body: bytes
    at: float


class Receiver:
    """A local HTTPS webhook receiver (the dev profile's certificate for 127.0.0.1)."""

    def __init__(self, tls: Path) -> None:
        self.deliveries: list[Delivery] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size)
                with outer._lock:
                    outer.deliveries.append(Delivery(dict(self.headers), body, time.time()))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls / "cert.pem", tls / "key.pem")
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self._server.server_port}/mapi-hook"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def snapshot(self) -> list[Delivery]:
        with self._lock:
            return list(self.deliveries)

    def wait_for(
        self,
        secret: str,
        event: str,
        match: Callable[[WebhookEnvelope], bool] = lambda _: True,
        *,
        after: int = 0,
    ) -> tuple[WebhookEnvelope, Delivery]:
        """The first verified delivery of ``event`` (from index ``after``) that matches."""
        deadline = time.monotonic() + EVENT_TIMEOUT_S
        while time.monotonic() < deadline:
            for d in self.snapshot()[after:]:
                env = verify(d.headers, d.body, secret)  # raises on a bad signature
                if env.type == event and match(env):
                    return env, d
            time.sleep(0.25)
        seen = [d.headers.get("Mapi-Event-Type") for d in self.snapshot()]
        pytest.fail(f"no signed {event} delivery within {EVENT_TIMEOUT_S:.0f} s (got {seen})")


# =============================================================================== journey


@dataclass
class Journey:
    """One analyst's workspace and everything the steps create in it."""

    stack: Stack
    email: str
    tenant: str = ""
    id_token: str = ""
    client: Client | None = None
    receiver: Receiver | None = None
    webhook_id: str = ""
    webhook_secret: str = ""
    csv: Path | None = None
    dataset_ref: str = ""
    spec_id: str = ""
    revision: int = 0
    translation_run: str = ""
    tiny: ModelVersion | None = None
    version: ModelVersion | None = None
    fit_runs: list[str] = field(default_factory=list)
    alias: str = "geo-mmm"
    endpoint_revision: int = 0
    score_token: str = ""
    score_run: str = ""
    saved_plot: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def api(self) -> Client:
        return _need(self.client, "the signed-in workspace")

    @property
    def project(self) -> str:
        return f"{self.tenant}/default"

    @property
    def mv(self) -> ModelVersion:
        return _need(self.version, "the promoted model version")

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.id_token}", PROJECT_HEADER: self.project}


def _need[T](value: T | None, what: str) -> T:
    if value is None or value == "":
        pytest.skip(f"needs {what} (an earlier step failed)")
    return value


class _Timer:
    def __init__(self, journey: Journey, name: str) -> None:
        self.journey, self.name = journey, name

    def __enter__(self) -> None:
        self.t0 = time.monotonic()

    def __exit__(self, *exc: object) -> None:
        self.journey.timings[self.name] = round(time.monotonic() - self.t0, 2)


@pytest.fixture(scope="module")
def journey(stack: Stack) -> Iterator[Journey]:
    email = os.environ.get("MAPI_E2E_GUIDED_ACCOUNT", "viewer@dev.local").strip().lower()
    j = Journey(stack=stack, email=email)
    j.receiver = Receiver(stack.webhook_tls)
    try:
        yield j
    finally:
        j.receiver.close()
        if j.client is not None:
            j.client.close()
        out = stack.data_dir / "e2e" / "m3-guided-timings.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        report = {"tenant": j.tenant, "timings_s": j.timings, "notes": j.notes}
        out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"\nM3 journey timings ({out}):\n{json.dumps(j.timings, indent=2)}")


def _operator(stack: Stack) -> Client:
    return Client(stack.api, token=dev_idp_login(stack.api, OPERATOR))


def _wait_run(api: Client, run_id: str) -> Run:
    final = api.runs.wait(run_id, timeout=RUN_TIMEOUT_S)
    assert final.state.value == "succeeded", final.problem
    return final


def _json_artifact(link_url: str) -> bytes:
    r = httpx.get(link_url, timeout=60)
    assert r.status_code == 200, r.text
    return r.content


# ====================================================================== 1. sign-up


def test_sign_up_verify_and_sign_in(journey: Journey) -> None:
    st, j = journey.stack, journey
    with httpx.Client(base_url=st.api, timeout=60) as raw, _Timer(j, "signup_to_signed_in"):
        org = f"M3 Journey {uuid.uuid4().hex[:6]}"
        r = raw.post(
            "/auth/signup",
            json={"email": j.email, "name": "M3 Analyst", "organisation": org,
                  "accepted_terms": True, "terms_version": TERMS_VERSION},
        )  # fmt: skip
        if r.status_code == 429:
            pytest.fail(
                "the stack's sign-up limits are used up (5 per address and hour, 25 per domain "
                "and day by default): restart it with higher MAPI_SIGNUP_PER_IP_PER_HOUR and "
                f"MAPI_SIGNUP_PER_DOMAIN_PER_DAY to run the e2e suites repeatedly ({r.text})"
            )
        assert r.status_code == 202 and r.json()["status"] == "check_email", r.text
        mail = raw.get("/dev/outbox", params={"to": j.email, "limit": 1}).json()["items"][0]
        token = mail["tags"]["token"]
        assert token in mail["body"] and "verify" in mail["body"].lower()
        r = raw.post("/auth/verify-email", json={"token": token})
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["trust_tier"] == "public" and result["project_id"] == "default"
        j.tenant = result["tenant_id"]
        j.id_token = dev_idp_login(st.api, j.email)
    with _operator(st) as op:  # (dev) the local-process executor runs internal tenants only
        tenant = op.admin.update_tenant(j.tenant, {"trust_tier": "internal"})
        assert tenant.trust_tier.value == "internal"
    j.client = Client(st.api, token=j.id_token)
    j.client.use(j.project)
    roles = {t.tenant.id: t.role.value if t.role else None for t in j.client.me().tenants}
    assert roles.get(j.tenant) == "admin"
    receiver = _need(j.receiver, "the webhook receiver")
    created = j.client.webhooks.create(receiver.url, list(EVENTS), description="m3 e2e")
    assert created.secret.startswith("whsec_")
    j.webhook_id, j.webhook_secret = created.webhook.id, created.secret


# ================================================================ 2. guided data path


def _spreadsheet(tmp: Path) -> tuple[Path, pd.DataFrame]:
    """The analyst's CSV: one row per week and region, a column per channel's spend."""
    ds, _ = _simulate(0, SIZE, SIZES[SIZE]["weeks"])
    df = ds.to_dataframe().reset_index()
    spend = df.pivot_table(index=["week", "region"], columns="channel", values="spend")
    rest = df.groupby(["week", "region"])[["revenue", "avg_price"]].first()
    table = rest.join(spend).reset_index().rename(columns=COLUMNS)
    table["Week"] = table["Week"].dt.strftime("%Y-%m-%d")
    path = tmp / "weekly_media_by_region.csv"
    table.to_csv(path, index=False)
    return path, table


def test_template_roles_draft_and_upload(
    journey: Journey, tmp_path_factory: pytest.TempPathFactory
) -> None:
    j = journey
    api = j.api
    templates = {t.id: t for t in api.guided.templates()}
    assert {"geo_mmm", "weekly_forecast", "store_demand", "customer_choice"} <= set(templates)
    csv, table = _spreadsheet(tmp_path_factory.mktemp("m3-data"))
    j.csv = csv
    assert len(table) == 5 * 104 and csv.stat().st_size < 64 * 1024
    with _Timer(j, "detect_roles"):
        profiles = profile_csv(csv)
        suggestions = api.guided.detect_roles(TEMPLATE, columns=profiles)
    answers = {q.question_id: q.answer for q in suggestions.questions}
    assert answers["kpi"] == "Revenue" and answers["date"] == "Week"
    assert answers["geo"] == "Region" and answers["controls"] == ["Avg Price"]
    assert set(answers["spend"]) == {"TV Spend", "Search Spend", "Social Spend"}  # type: ignore[arg-type]
    assert not [i for i in suggestions.issues if i.severity == "error"]
    for q in suggestions.questions:  # every pre-filled answer says why, in plain words
        for cand in q.candidates:
            assert cand.reason_plain and 0.0 <= cand.confidence <= 1.0
    with _Timer(j, "draft_and_upload"):
        draft = api.guided.draft(
            template_id=TEMPLATE,
            name="geo-mmm",
            dataset="weekly-media",
            answers=answers,
            columns=[p.model_dump(mode="json") for p in profiles],
            business_priors=BUSINESS_PRIORS,
            lift_tests=LIFT_TESTS,
        )
        assert [i for i in draft.issues if i.severity == "error"] == [], draft.issues
        assert "kind: ModelSpec" in draft.yaml and draft.spec["calibration"]
        version = api.datasets.upload(
            csv, dataset="weekly-media", mapping=draft.mapping, timeout=RUN_TIMEOUT_S
        )
    assert version.status.value == "ready", version
    j.dataset_ref = str(version.ref)
    # the stored version's column profiles (columns.json, written by the sandbox) detect alike
    stored = api.guided.detect_roles(TEMPLATE, dataset=j.dataset_ref)
    assert {q.question_id: q.answer for q in stored.questions} == answers
    doc = dict(draft.spec)
    doc["model"]["fit"].update(TINY_FIT)  # type: ignore[index,union-attr]
    spec = api.specs.create(yaml.safe_dump(doc, sort_keys=False), message="m3 e2e: guided draft")
    j.spec_id, j.revision = spec.id, spec.latest_revision or 1


def test_the_same_data_as_an_mff_long_table(journey: Journey) -> None:
    """MFF (the model feature format long table) ingests to the same variables."""
    j = journey
    api, csv = j.api, _need(j.csv, "the CSV")
    long = pd.read_csv(csv).melt(
        id_vars=["Week", "Region"], var_name="VariableName", value_name="VariableValue"
    )
    long = long.rename(columns={"Week": "Period", "Region": "Geography"})
    long["VariableName"] = long["VariableName"].str.replace(" ", "_")
    mff = csv.with_name("weekly_media_mff.csv")
    long.to_csv(mff, index=False)
    answers = {"kpi": "Revenue", "date": "Period", "geo": "Geography",
               "spend": ["TV_Spend", "Search_Spend", "Social_Spend"],
               "controls": ["Avg_Price"], "frequency": "weekly", "holdout_periods": 8}  # fmt: skip
    draft = api.guided.draft(
        template_id=TEMPLATE, name="geo-mmm-mff", dataset="weekly-media-mff", format="mff",
        answers=answers,
    )  # fmt: skip
    assert draft.mapping.format == "mff" and not [i for i in draft.issues if i.severity == "error"]
    with _Timer(j, "mff_upload"):
        got = api.datasets.upload(mff, dataset="weekly-media-mff", mapping=draft.mapping)
    assert got.status.value == "ready", got
    ref = DatasetRefParts(j.dataset_ref)
    csv_version = api.datasets.version(ref.dataset_id(api), ref.version)
    a, b = got.data_schema, csv_version.data_schema
    assert a is not None and b is not None

    def shape(schema: Any) -> dict[str, tuple[str | None, int]]:
        return {n: (v.role.value if v.role else None, len(v.dims)) for n, v in schema.vars.items()}

    assert shape(a) == shape(b) == {"revenue": ("target", 2), "spend": ("spend", 3),
                                    "avg_price": ("control", 2)}  # fmt: skip


@dataclass(frozen=True)
class DatasetRefParts:
    ref: str

    @property
    def name(self) -> str:
        return self.ref.split("/", 3)[3].split("@")[0]

    @property
    def version(self) -> int:
        return int(self.ref.rsplit("@v", 1)[1])

    def dataset_id(self, api: Client) -> str:
        found = api.datasets.find(self.name)
        assert found is not None, self.ref
        return found.id


# ================================================================ 3. business priors


def test_business_priors_preview_and_lift_test(journey: Journey) -> None:
    j = journey
    api = j.api
    spec_id = _need(j.spec_id, "the saved spec")
    t = truth(0, SIZE)
    assert 1.5 <= t.roi["tv"] <= 4.0 and 0.06 <= t.share["search"] <= 0.2  # beliefs are true
    with _Timer(j, "translate_priors"):
        tr = api.guided.preview_priors(
            BUSINESS_PRIORS, spec_id=spec_id, revision=j.revision, lift_tests=LIFT_TESTS,
            draws=500, timeout=RUN_TIMEOUT_S,
        )  # fmt: skip
    result = _need(tr.result, "the translation result")
    assert set(result.priors["roi"]) == {"tv", "search"}  # mmm-framework ROI priors
    # the translated priors put about the stated credibility inside each range
    for key in ("tv", "search", "baseline"):
        assert abs(result.coverage[key] - 0.8) < 0.15, result.coverage
    preview = result.preview.model_dump(mode="json")
    assert preview, "the translation previews its priors in business units"
    j.notes["prior_coverage"] = result.coverage
    doc = api.specs.get(spec_id).latest.document  # type: ignore[union-attr]
    applied = apply_prior_translation(
        doc, result, business_priors=BusinessPriors.model_validate(BUSINESS_PRIORS),
        run_id=tr.run.id,
    )  # fmt: skip
    assert applied["calibration"]["lift_tests"][0]["id"] == "tv-north"
    rev = api.specs.revise(spec_id, yaml.safe_dump(applied, sort_keys=False), message="priors")
    j.revision, j.translation_run = rev.revision, tr.run.id


# ================================================================== 4. fit, scorecard


def _fit(j: Journey, fit: Mapping[str, Any], message: str, timer: str) -> ModelVersion:
    api = j.api
    doc = api.specs.get(j.spec_id).latest.document  # type: ignore[union-attr]
    doc["model"]["fit"] = {**doc["model"]["fit"], **fit}
    rev = api.specs.revise(j.spec_id, yaml.safe_dump(doc, sort_keys=False), message=message)
    with _Timer(j, timer):
        run = api.runs.fit(j.spec_id, rev.revision, labels={"suite": "m3-e2e"})
        final = _wait_run(api, run.id)
    j.fit_runs.append(final.id)
    mv_id = _need(final.output_model_version_id, "the fit's model version")
    model = next(m for m in api.models.list() if m.name == "geo-mmm")
    found = next(v for v in api.models.versions(model.id) if v.id == mv_id)
    return api.models.version(model.id, found.version)


def _gate(mv: ModelVersion) -> str:
    """The promotion gate ``mapi_spec`` derives from the version's scorecard (a Guided fit is
    ``evaluate_then_refit``, the endpoints' default policy)."""
    card = _need(mv.scorecard, "the version's scorecard")
    return promotion_gate(Scorecard.model_validate(card.model_dump())).outcome.value


def test_tiny_fit_scorecard_and_its_gate(journey: Journey) -> None:
    j = journey
    _need(j.translation_run, "the translated priors")
    mv = _fit(j, TINY_FIT, "m3 e2e: tiny fit", "fit_tiny")
    j.tiny = mv
    card = _need(mv.scorecard, "the scorecard")
    template = j.api.guided.template(TEMPLATE)
    assert {i.check for i in card.items} == set(template.scorecard_checks)
    for item in card.items:  # every check speaks plain language and points at the docs
        assert item.message_plain and item.docs_anchor and item.status in ("pass", "warn", "fail")
        if item.status != "pass":
            assert item.suggested_action
    assert {i.check: i.status for i in card.items}["data.history"] == "pass"
    gate = _gate(mv)
    j.notes["tiny_scorecard"] = {i.check: i.status for i in card.items}
    j.notes["tiny_gate"] = gate
    # the server's gate is the spec's: try the promotion and read the answer
    try:
        j.api.endpoints.promote("geo-mmm-tiny", mv.ref, expected_revision=0)
    except MapiError as err:
        want = {
            "blocked": ErrorCode.PROMOTION_BLOCKED,
            "needs_override": ErrorCode.OVERRIDE_REQUIRED,
        }
        assert err.code is want[gate], err
        checks = {i.context.get("check") for i in err.issues if i.context}
        assert checks <= {i.check for i in card.items if i.status != "pass"} | {None}
    else:
        assert gate == "allowed"
        j.api.endpoints.delete("geo-mmm-tiny")


def test_the_endpoint_fit(journey: Journey) -> None:
    j = journey
    _need(j.tiny, "the tiny fit")
    mv = _fit(j, ENDPOINT_FIT, "m3 e2e: endpoint fit", "fit_endpoint")
    j.version = mv
    j.notes["endpoint_scorecard"] = {
        i.check: i.status for i in (mv.scorecard.items if mv.scorecard else [])
    }
    assert _gate(mv) in ("allowed", "needs_override"), mv.scorecard
    assert {"ui/grids/response_curves.json", "scorecard.json"} <= {a.name for a in mv.artifacts}


# ===================================================================== 5. what-if


def _grid(j: Journey) -> ResponseGridView:
    mv = j.mv
    link = j.api.models.artifact(mv.model_id, mv.version, "ui/grids/response_curves.json")
    return ResponseGridView.model_validate_json(_json_artifact(link.url))


def _view(outputs: Sequence[Any], name: str) -> bytes:
    link = next(o for o in outputs if o.name == name)
    return _json_artifact(link.url)


def test_whatif_grid_agrees_with_run_exact_at_grid_points(journey: Journey) -> None:
    j = journey
    api, mv = j.api, j.mv
    grid = _grid(j)
    assert set(grid.channels) == {"tv", "search", "social"} and grid.periods == 104
    assert 0.5 in grid.multipliers and 1.5 in grid.multipliers
    agree: dict[str, Any] = {}
    for multipliers in ({"tv": 0.5}, {"search": 1.5, "tv": 0.8}):
        scenario = WhatIfScenario(multipliers=multipliers)
        with _Timer(j, "whatif_instant"):
            instant = evaluate_scenario(grid, scenario)
        request = api.guided.compile(
            {"kind": "whatif", "model": mv.ref, "scenario": scenario.model_dump(mode="json")}
        )
        # "Run exact" scores the data the version was fitted on (named by the server)
        assert request.mode == "counterfactual" and request.data is not None
        assert str(request.data).startswith("dataset://default/weekly-media@v")
        with _Timer(j, f"run_exact_{'_'.join(sorted(multipliers))}"):
            exact = api.models.score(mv.model_id, mv.version, request, download=False,
                                     timeout=RUN_TIMEOUT_S)  # fmt: skip
        view = CounterfactualView.model_validate_json(
            _view(exact.outputs, "ui/guided/counterfactual.json")
        )
        diff = abs(instant.kpi_delta.mean - view.delta_total.mean)
        tol = max(0.25 * view.delta_total.sd, 0.02 * abs(view.delta_total.mean))
        agree[json.dumps(multipliers)] = {
            "instant": instant.kpi_delta.mean, "exact": view.delta_total.mean,
            "exact_sd": view.delta_total.sd, "abs_diff": diff,
        }  # fmt: skip
        assert diff <= tol, agree
        assert view.delta_total.mean < 0 if multipliers == {"tv": 0.5} else True
    j.notes["whatif_vs_exact"] = agree
    # sliders never extrapolate beyond the grid ("Run exact" covers that)
    with pytest.raises(MapiError) as err:
        evaluate_scenario(grid, WhatIfScenario(multipliers={"tv": grid.multipliers[-1] + 1}))
    assert err.value.code is ErrorCode.SCENARIO_OUT_OF_RANGE


def test_forecast_and_channel_off(journey: Journey) -> None:
    j = journey
    api, mv = j.api, j.mv
    req = api.guided.compile({"kind": "forecast", "model": mv.ref, "periods": 8, "dim": "time"})
    assert req.future_covariates == "carry_last" and req.horizon is not None
    with _Timer(j, "forecast"):
        res = api.models.score(mv.model_id, mv.version, req, download=False, timeout=RUN_TIMEOUT_S)
    fc = ForecastView.model_validate_json(_view(res.outputs, "ui/guided/forecast.json"))
    assert len(fc.fan.coords) == fc.history + 8 and fc.split_at is not None
    assert fc.total is not None and fc.total.mean > 0
    off = api.guided.compile(
        {
            "kind": "counterfactual",
            "model": mv.ref,
            "preset": {"kind": "channel_off", "channel": "tv"},
        }
    )
    assert off.data is not None  # over the fit window
    with _Timer(j, "counterfactual_tv_off"):
        res = api.models.score(mv.model_id, mv.version, off, download=False, timeout=RUN_TIMEOUT_S)
    cf = CounterfactualView.model_validate_json(_view(res.outputs, "ui/guided/counterfactual.json"))
    assert cf.delta_total.mean < 0 and cf.delta_share is not None
    # the grid's multiplier 0 is the same question
    at_zero = evaluate_scenario(_grid(j), WhatIfScenario(multipliers={"tv": 0.0}))
    assert abs(at_zero.kpi_delta.mean - cf.delta_total.mean) <= max(
        0.25 * cf.delta_total.sd, 0.02 * abs(cf.delta_total.mean)
    )
    j.notes["tv_off"] = {"exact": cf.delta_total.mean, "grid": at_zero.kpi_delta.mean}


# ===================================================================== 6. endpoint


def test_promote_through_the_gate(journey: Journey) -> None:
    j = journey
    api, mv = j.api, j.mv
    gate = _gate(mv)
    with _Timer(j, "promote"):
        if gate == "needs_override":
            with pytest.raises(MapiError) as err:
                api.endpoints.promote(j.alias, mv.ref, expected_revision=0)
            assert err.value.code is ErrorCode.OVERRIDE_REQUIRED
            gate_checks = {i.context.get("check") for i in err.value.issues if i.context}
            warned = {
                i.check for i in (mv.scorecard.items if mv.scorecard else []) if i.status == "warn"
            }
            assert gate_checks and gate_checks <= warned
            ep = api.endpoints.promote(
                j.alias, mv.ref, override=True, expected_revision=0,
                reason="m3 e2e: convergence warnings reviewed for a demonstration model",
            )  # fmt: skip
            assert ep.revision == 1
        else:
            ep = api.endpoints.promote(j.alias, mv.ref, expected_revision=0)
    j.endpoint_revision = ep.revision
    history = list(api.endpoints.history(j.alias))
    assert [h.revision for h in history] == [1] and history[0].model_version_id == mv.id
    schema = api.endpoints.schema(j.alias)
    assert schema.model_ref == mv.ref and schema.example is not None
    snippets = api.endpoints.snippets(j.alias)
    languages = {s.language for s in snippets.items}
    assert {"python", "curl", "apps_script", "notebook"} <= {
        str(getattr(x, "value", x)) for x in languages
    }
    for s in snippets.items:
        assert j.id_token not in s.code and "whsec_" not in s.code  # snippets carry no secret


def test_score_through_the_endpoint_with_a_score_token(journey: Journey) -> None:
    j = journey
    api = j.api
    _need(j.endpoint_revision, "the endpoint")
    created = api.tokens.create(f"score-{j.alias}", [f"score:{j.alias}"], expires_in_days=1)
    j.score_token = created.secret
    with Client(j.stack.api, token=created.secret) as scorer:
        scorer.use(j.project)
        schema = scorer.endpoints.schema(j.alias)
        with _Timer(j, "endpoint_score"):
            res = scorer.endpoints.score(j.alias, schema.example, timeout=RUN_TIMEOUT_S)
        assert res.run.state.value == "succeeded"
        j.score_run = res.run.id
        tree = res.tree
        assert tree is not None and "summary" in tree.children
        y = tree["summary"].to_dataset()["y_hat"]
        horizon = next(d for d in y.dims if d not in ("stat", "geo", "region"))
        assert y.sizes[horizon] == 8
        # the token can do this and nothing else
        auth = {"Authorization": f"Bearer {created.secret}", PROJECT_HEADER: j.project}
        for path in ("/v1/datasets", "/v1/specs", "/v1/webhooks", f"/v1/models/{j.mv.model_id}"):
            assert httpx.get(f"{j.stack.api}{path}", headers=auth).status_code == 403, path


# ===================================================================== 7. webhooks


def test_webhooks_are_signed_and_replayable(journey: Journey) -> None:
    j = journey
    receiver = _need(j.receiver, "the receiver")
    secret = _need(j.webhook_secret, "the webhook")
    fit_run = j.fit_runs[-1] if j.fit_runs else _need(None, "the endpoint fit")
    mv = j.mv
    with _Timer(j, "webhooks_received"):
        run_ok, _ = receiver.wait_for(
            secret, "run.succeeded", lambda e: e.data["run_id"] == fit_run
        )
        ready, _ = receiver.wait_for(
            secret, "model.version.ready", lambda e: e.data["model_version_id"] == mv.id
        )
        promoted, _ = receiver.wait_for(
            secret, "endpoint.promoted", lambda e: e.data["alias"] == j.alias
        )
        scored, scored_at = receiver.wait_for(
            secret, "score.completed", lambda e: e.data["run_id"] == j.score_run
        )
    assert run_ok.tenant == j.tenant and run_ok.data["kind"] == "fit"
    assert ready.data["scorecard_overall"] in ("pass", "warn")
    assert promoted.data["revision"] == 1 and promoted.data["overridden"] is (
        _gate(mv) == "needs_override"
    )
    assert scored.data["endpoint"]["alias"] == j.alias
    for d in receiver.snapshot():  # ids and states only: no values, links or secrets
        assert (
            b"https://" not in d.body.replace(receiver.url.encode(), b"")
            and b"whsec_" not in d.body
        )
    # the delivery log shows them, and a replay is the same event signed again
    log = list(j.api.webhooks.deliveries(j.webhook_id))
    assert {d.status.value for d in log if d.event_id == scored.id} == {"succeeded"}
    first = next(d for d in log if d.event_id == scored.id)
    before = len(receiver.snapshot())
    replay = j.api.webhooks.replay(j.webhook_id, first.id)
    assert replay.replay_of == first.id and replay.id != first.id
    again, d = receiver.wait_for(
        secret, "score.completed", lambda e: e.id == scored.id, after=before
    )
    assert again == scored and d.headers.get("Mapi-Delivery-Id") == replay.id
    assert d.headers.get("Mapi-Delivery-Id") != scored_at.headers.get("Mapi-Delivery-Id")
    # a body signed with another secret is refused by the verifier
    with pytest.raises(MapiError):
        verify(d.headers, d.body, "whsec_" + "x" * 43)


# ======================================================================= 8. embed


def test_an_embed_link_renders_read_only_and_is_revoked(journey: Journey) -> None:
    j = journey
    api, mv = j.api, j.mv
    plots = {p.id: p for p in api.plots.list(mv.model_id, mv.version)}
    assert "mmm.response_curves" in plots
    saved = api.plots.save(
        "Response curves", model_id=mv.model_id, version=mv.version, plot_id="mmm.response_curves"
    )
    assert saved.embeddable and saved.provenance == "recipe"
    j.saved_plot = saved.id
    link = api.plots.create_embed(saved.id, expires_in_days=1, label="m3 e2e")
    assert link.url.startswith(j.stack.local.plots_origin.rstrip("/") + "/embed/")
    with httpx.Client(timeout=30) as anon:  # no credentials at all
        view = anon.get(f"{j.stack.api}/v1/embeds/{link.token}")
        assert view.status_code == 200, view.text
        body = view.json()
        assert body["title"] == "Response curves" and body["plot"]["kind"] == "vega-lite"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert anon.request(method, f"{j.stack.api}/v1/embeds/{link.token}").status_code == 405
        page = anon.get(link.url)
        assert page.status_code == 200
        csp = page.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp and "/v1/embeds/" in csp
        api.plots.revoke_embed(saved.id, link.embed.id)
        gone = anon.get(f"{j.stack.api}/v1/embeds/{link.token}")
        assert gone.status_code == 410 and gone.json()["code"] == "LINK_EXPIRED"


# ============================================================= 9. budget optimisation


def test_the_platform_optimises_the_budget(journey: Journey) -> None:
    """``POST …:optimize`` (v0.5): mmm-framework planning over
    the promoted version's posterior reallocates the budget with tv held within +-50%; the
    plan spends the budget, does not lose KPI on average and reports its frontier."""
    j = journey
    mv = j.mv
    body = {
        "budget_change_pct": 0,
        "channels": {"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5}},
        "objective": "mean",
        "frontier": {"n_points": 5},
        "max_draws": 100,
    }
    url = f"{j.stack.api}/v1/models/{mv.model_id}/versions/{mv.version}:optimize"
    with httpx.Client(timeout=90, verify=False) as http, _Timer(j, "optimize"):
        r = http.post(url, json=body, headers={**j.headers(), "Prefer": "wait=60"})
        assert r.status_code in (200, 202), r.text
        view = r.json()
        run_id = view["run"]["id"]
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while not view["done"] and time.monotonic() < deadline:
            time.sleep(2)
            got = http.get(f"{j.stack.api}/v1/optimizations/{run_id}", headers=j.headers())
            assert got.status_code == 200, got.text
            view = got.json()
    assert view["done"] and view["result"] is not None, view["run"].get("problem")
    plan = view["result"]
    spent = sum(a["optimal_spend"] for a in plan["allocation"])
    assert spent == pytest.approx(plan["total_budget"], rel=0.005)
    assert plan["uplift"]["mean"] >= -1e-6 * abs(plan["expected_kpi"]["mean"])
    assert len(plan["frontier"]) == 5 and plan["kpi_units"]
    j.notes["optimize"] = {
        "allocation": {a["channel"]: a["optimal_spend"] for a in plan["allocation"]},
        "uplift": plan["uplift"],
        "prob_positive_uplift": plan["prob_positive_uplift"],
    }


# ========================================================= 10. export and deletion


def test_gdpr_export_downloads(journey: Journey, tmp_path: Path) -> None:
    j = journey
    api = j.api
    _need(j.webhook_secret, "the workspace")
    with _Timer(j, "gdpr_export"):
        started = api.tenants.export(j.tenant)
        path = api.tenants.download_export(
            j.tenant, started.id, tmp_path / "export.zip", timeout=RUN_TIMEOUT_S
        )
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert {"README.txt", "manifest.json", "metadata/projects.jsonl"} <= names
        blob = b"".join(zf.read(n) for n in names if n.endswith((".json", ".jsonl", ".txt")))
    assert f'"tenant_id": "{j.tenant}"'.encode() in blob
    for secret in (j.webhook_secret, j.score_token, j.id_token):  # credentials are redacted
        assert secret and secret.encode() not in blob
    assert any(n.startswith("objects/") or "datasets" in n for n in names), sorted(names)[:40]
    j.notes["export_files"] = len(names)


def _tenant_rows(stack: Stack, tenant: str) -> dict[str, int]:
    """Rows per table with a ``tenant_id`` column (as the database owner: no RLS)."""
    with psycopg.connect(stack.database_url, autocommit=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name = 'tenant_id' AND table_schema = 'public'"
            ).fetchall()
        ]
        counts = {}
        for t in tables:
            row = conn.execute(f'SELECT count(*) FROM "{t}" WHERE tenant_id = %s', (tenant,))
            counts[t] = int(row.fetchone()[0])  # type: ignore[index]
    return counts


def _tenant_objects(stack: Stack, tenant: str) -> dict[str, int]:
    async def count() -> dict[str, int]:
        store = ShimObjectStore.from_root(stack.data_dir / "gcs", endpoint=stack.local.gcs_endpoint)
        try:
            out = {}
            for bucket in stack.local.buckets.all().values():
                n, token = 0, None
                while True:
                    page = await store.list(bucket, prefix=f"t/{tenant}/", page_token=token)
                    n += len(page.items)
                    token = page.next_page_token
                    if not token:
                        break
                out[bucket] = n
            return out
        finally:
            await store.aclose()

    return asyncio.run(count())


def _bundles_retention(stack: Stack) -> int:
    """The bundles bucket's retention period in the shim (0: none)."""

    async def get() -> int:
        store = ShimObjectStore.from_root(stack.data_dir / "gcs", endpoint=stack.local.gcs_endpoint)
        try:
            meta = await store.get_bucket(stack.local.buckets.bundles)
        finally:
            await store.aclose()
        return int((meta.get("retentionPolicy") or {}).get("retentionPeriod") or 0)

    return asyncio.run(get())


def test_tenant_deletion_purges_everything(journey: Journey) -> None:
    """Every tenant row (but the retained audit, cost and deletion records and the tombstone)
    and every object under ``t/<tenant>/``. Model bundles sit in a bucket with a locked
    retention policy (30 days when deployed): on a stack whose shim keeps one
    (``MAPI_BUNDLE_RETENTION_DAYS`` unset or positive) the purge stops at them, records each
    with the time it may be deleted and resumes then - checked here, then skipped. Start the
    stack with ``MAPI_BUNDLE_RETENTION_DAYS=0`` (a fresh ``.local``) to see it finish."""
    from mapi_server.services.deletion import RETAINED_TABLES

    j = journey
    api = j.api
    _need(j.webhook_secret, "the workspace")
    before_rows = _tenant_rows(j.stack, j.tenant)
    before_objects = _tenant_objects(j.stack, j.tenant)
    assert sum(before_objects.values()) > 0 and before_rows.get("runs", 0) > 0
    j.notes["before_purge"] = {"rows": sum(before_rows.values()), "objects": before_objects}
    receiver = _need(j.receiver, "the receiver")
    mark = len(receiver.snapshot())
    locked = _bundles_retention(j.stack) > 0
    with _Timer(j, "tenant_deletion"):
        deletion = api.tenants.request_deletion(j.tenant, confirm=j.tenant, reason="m3 e2e")
        if deletion.purge_after > deletion.requested_at:
            api.tenants.cancel_deletion(j.tenant)
            pytest.skip("the purge is checked on stacks with MAPI_TENANT_DELETION_GRACE_S=0")
        with _operator(j.stack) as op:
            deadline = time.monotonic() + RUN_TIMEOUT_S
            while True:
                doc = op.tenants.deletion(j.tenant)
                stores = {str(st.store): st for st in doc.stores}
                if doc.state.value == "purged":
                    break
                if (
                    locked
                    and stores["object_store"].detail
                    and "retention" in (stores["object_store"].detail or "")
                ):
                    break
                assert doc.state.value != "failed", doc
                assert time.monotonic() < deadline, f"the purge did not finish: {doc}"
                time.sleep(1.0)
    assert stores["database"].state == "done"
    rows = _tenant_rows(j.stack, j.tenant)
    leftover = {t: n for t, n in rows.items() if n and t not in RETAINED_TABLES}
    assert leftover == {}, leftover
    assert {t for t, n in rows.items() if n} <= set(RETAINED_TABLES)
    j.notes["after_purge_retained_rows"] = {t: n for t, n in rows.items() if n}
    objects = _tenant_objects(j.stack, j.tenant)
    # the workspace is gone for its admin, and the score token is dead
    gone = httpx.get(f"{j.stack.api}/v1/datasets", headers=j.headers())
    assert gone.status_code in (401, 403, 404)
    auth = {"Authorization": f"Bearer {j.score_token}", PROJECT_HEADER: j.project}
    assert httpx.get(f"{j.stack.api}/v1/endpoints/{j.alias}", headers=auth).status_code == 401
    if doc.state.value != "purged":
        bundles = j.stack.local.buckets.bundles
        assert {b: n for b, n in objects.items() if n} == {bundles: objects[bundles]}
        assert stores["object_store"].items_done == sum(before_objects.values()) - objects[bundles]
        j.notes["purge_waits_for_bundle_retention"] = stores["object_store"].detail
        pytest.skip(
            f"{objects[bundles]} bundle objects wait for their retention lock "
            f"({stores['object_store'].detail}); run the stack with MAPI_BUNDLE_RETENTION_DAYS=0"
        )
    assert sum(objects.values()) == 0, objects
    done, _ = receiver.wait_for(j.webhook_secret, "tenant.deletion.completed", after=mark)
    assert done.tenant == j.tenant and done.project is None
