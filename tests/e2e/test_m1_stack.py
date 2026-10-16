"""The M1 exit criterion, through the *running* local stack (``make dev``, then ``make e2e``).

Fit an E33-style hierarchical geo MMM from uploaded data, follow its SSE stream to
``succeeded``, prove the finalization (signed manifest bound to the bundles bucket's
generations, a bundle the SDK can read back with every hash intact), score an 8-week
horizon, a new market, a weekly EUR plan entering at ``inputs_stage`` and two
counterfactuals with the corpus' own expectations, read the plots, ``ui/`` views, scorecard
and diagnostics, and probe the security boundaries that are observable from outside:
the bootstrap object is gone, forged events are refused, the exec principal cannot reach
bundles.

Nothing here starts services. The suite reads the same environment as ``scripts/dev.py``
(:class:`mapi_spec.localdev.LocalDev`: ``MAPI_API_URL``, ``MAPI_GCS_ENDPOINT``,
``MAPI_DATA_DIR``, ``MAPI_ENV``, ``DATABASE_URL``) to find the API, the GCS shim, the shim's
key files and the database. The uploads, the fit and the bundle are module-scoped: they run
once and every test reads them.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import psycopg
import pytest
import xarray as xr
import yaml
from mapi_client import Client, RunEnd, RunEventFrame, RunLiveFrame, RunSnapshot, RunStreamItem
from mapi_client.errors import TransportError
from mapi_conformance.fixtures.e33 import data as e33
from mapi_conformance.fixtures.e33.expectations import check_fit, check_score
from mapi_conformance.runner import FixtureContext
from mapi_conformance.stack import StackAuth
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_server.services.signing import BundleSigner
from mapi_spec.api import ModelVersion, Run
from mapi_spec.executor import RunEvent, RunEventType
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.localdev import LocalDev
from mapi_spec.run import BundleManifest, sign_event
from mapi_spec.storage import bundle_prefix
from mapi_spec.views import PreprocessView, TreeIndexView

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[2]
SEED = 0
SIZE: Final = "tiny"
E2E_PREFIX = "e2e"

#: Fixture refs in the E33 spec / score requests -> (dataset name, payload builder or table).
SPEC_TABLES: dict[str, tuple[str, str]] = {
    "dataset://growth/eu_media_daily@latest": ("e2e_eu_media_daily", "data"),
    "dataset://growth/fx_daily@latest": ("e2e_fx_daily", "fx"),
    "dataset://shared/holidays_eu@v3": ("e2e_holidays_eu", "holidays"),
}
PAYLOADS: dict[str, str] = {
    "future": "e2e_future",
    "future_new_market": "e2e_future_new_market",
    "weekly_plan": "e2e_weekly_plan",
    "window": "e2e_window",
    "window_tv_plan": "e2e_window_tv_plan",
}
SCENARIO_REFS: dict[str, str] = {
    "dataset://growth/window@v1": "window",
    "dataset://growth/tv_plan@v1": "window_tv_plan",
}


@dataclass(frozen=True)
class Stack:
    """Where the running stack lives (resolved like ``scripts/dev.py`` does)."""

    local: LocalDev
    data_dir: Path

    @property
    def gcs_keys(self) -> Path:
        return self.data_dir / "gcs" / "_shim" / "keys"

    def shim_token(self, principal: str) -> str:
        path = self.gcs_keys / f"{principal}.json"
        if not path.is_file():
            pytest.fail(f"no shim key file at {path}: is MAPI_DATA_DIR the stack's data dir?")
        token = json.loads(path.read_text())["token"]
        assert isinstance(token, str) and token
        return token

    @property
    def signing_key(self) -> Path:
        return self.data_dir / "keys" / "bundle-signing-p256.pem"

    @property
    def database_url(self) -> str:
        """``DATABASE_URL``, or the embedded pgserver's unix-socket URL (``make dev``)."""
        if self.local.database_url:
            return self.local.database_url
        return f"postgresql://postgres:@/mapi?host={self.data_dir / 'pg'}"


@pytest.fixture(scope="session")
def stack() -> Stack:
    local = LocalDev.from_environ(os.environ)
    data_dir = Path(local.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return Stack(local=local, data_dir=data_dir)


@pytest.fixture(scope="session")
def api(stack: Stack, stack_credentials: StackAuth) -> Iterator[Client]:
    with Client(
        stack.local.api_url, token=stack_credentials.token, project=stack_credentials.project
    ) as client:
        try:
            ready = client.ready()
        except TransportError as exc:
            pytest.fail(
                f"the API is not reachable at {stack.local.api_url} ({exc}); start the stack "
                "with `make dev` first (export MAPI_API_URL if it is not on port 8000)"
            )
        assert ready.status == "ok", f"the stack is not ready: {ready.model_dump()}"
        yield client


@pytest.fixture(scope="session")
def token() -> str:
    """A per-session suffix so specs (and the models they register) never collide."""
    return uuid.uuid4().hex[:8]


# ----------------------------------------------------------------------------------- data


@dataclass
class E33Data:
    data: xr.Dataset
    side: dict[str, xr.Dataset]
    refs: dict[str, str] = field(default_factory=dict)
    """Fixture ref (spec / score request) or payload name -> uploaded ``dataset://`` ref."""


@pytest.fixture(scope="module")
def e33_data(api: Client) -> E33Data:
    """Upload the E33 tiny dataset, its side tables and every score payload (signed PUT →
    sandboxed ingest), exactly as a user of the API would."""
    data, side = e33.generate(SEED, SIZE)
    tables = {
        # the raw schema travels with the data (market: new_levels, roles, units)
        "data": data.assign_attrs({DATASET_SCHEMA_ATTR: e33.schema().model_dump_json()}),
        **side,
    }
    out = E33Data(data=data, side=side)
    for ref, (name, table) in SPEC_TABLES.items():
        version = api.datasets.upload(tables[table], dataset=name, timeout=600)
        assert version.status.value == "ready", version
        out.refs[ref] = version.ref
    for payload, name in PAYLOADS.items():
        ds = getattr(e33, f"payload_{payload}")(data, SEED, SIZE)
        version = api.datasets.upload(ds, dataset=name, timeout=600)
        assert version.status.value == "ready", version
        out.refs[payload] = version.ref
    return out


def e33_spec_yaml(refs: dict[str, str], name: str) -> str:
    """The corpus' E33 spec with uploaded dataset refs and a session-unique name."""
    text = (Path(e33.__file__).parent / "spec.yaml").read_text(encoding="utf-8")
    for old in SPEC_TABLES:
        assert old in text, f"the E33 fixture spec no longer references {old}"
        text = text.replace(old, refs[old])
    text = text.replace("name: eu-geo-mmm, project: growth", f"name: {name}, project: default")
    assert f"name: {name}" in text, "the E33 fixture spec metadata changed"
    return text


# ------------------------------------------------------------------------------------ fit


@dataclass
class FitResult:
    spec_id: str
    revision: int
    run: Run
    stream: list[RunStreamItem]
    version: ModelVersion


@pytest.fixture(scope="module")
def fit(api: Client, e33_data: E33Data, token: str) -> FitResult:
    """Validate and create the E33 spec, fit it, and follow the SSE stream to the end."""
    yaml = e33_spec_yaml(e33_data.refs, f"{E2E_PREFIX}-geo-mmm-{token}")
    verdict = api.specs.validate(yaml)
    assert verdict.valid, [i.model_dump() for i in verdict.issues]
    doc = api.specs.create(yaml, message="e2e: E33 tiny")
    run = api.runs.fit(doc.id, doc.latest_revision, labels={"suite": "e2e"})
    stream = list(api.runs.stream_events(run.id))
    end = stream[-1]
    assert isinstance(end, RunEnd), stream[-3:]
    final = end.run
    assert final.state.value == "succeeded", final.problem
    assert final.output_model_version_id
    model = next(m for m in api.models.list() if m.name == f"{E2E_PREFIX}-geo-mmm-{token}")
    version = api.models.version(model.id, model.latest_version or 1)
    assert version.id == final.output_model_version_id
    return FitResult(doc.id, doc.latest_revision, final, stream, version)


def frames(stream: list[RunStreamItem]) -> list[RunEventFrame | RunLiveFrame]:
    return [i for i in stream if isinstance(i, RunEventFrame | RunLiveFrame)]


def snapshots(stream: list[RunStreamItem]) -> list[Run]:
    return [i.run for i in stream if isinstance(i, RunSnapshot)]


def shim_request(stack: Stack, principal: str, method: str, path: str, **kw: Any) -> httpx.Response:
    """A JSON-API call to the GCS shim as ``principal`` (``control`` or ``exec``)."""
    headers = {"Authorization": f"Bearer {stack.shim_token(principal)}"}
    url = stack.local.gcs_endpoint.rstrip("/") + path
    return httpx.request(method, url, headers=headers, timeout=30, **kw)


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry()


@pytest.fixture(scope="module")
def bundle(
    api: Client, fit: FitResult, registry: Registry, tmp_path_factory: pytest.TempPathFactory
) -> Bundle:
    """The registered bundle pulled back through signed URLs and read by the SDK with every
    file hash verified against the SDK manifest (``bundle.manifest.json``)."""
    mv = fit.version
    root = tmp_path_factory.mktemp("bundle")
    for obj in mv.artifacts:
        if obj.name.startswith("ui/"):
            continue
        local = "manifest.json" if obj.name == "bundle.manifest.json" else obj.name
        dest = root / local
        dest.parent.mkdir(parents=True, exist_ok=True)
        api.models.download(mv.model_id, mv.version, obj.name, dest)  # size + CRC32C checked
    return Bundle.read(root, registry, verify=True)


# ===================================================================================== fit


def test_fit_streams_progress_to_succeeded(api: Client, fit: FitResult) -> None:
    stream = fit.stream
    assert isinstance(stream[0], RunSnapshot) and isinstance(stream[-1], RunEnd)
    persisted = [i for i in stream if isinstance(i, RunEventFrame)]
    live = [i for i in stream if isinstance(i, RunLiveFrame)]
    phases = [
        str(i.event.payload.get("phase")) for i in persisted if i.event.type is RunEventType.STATE
    ]
    assert {"bootstrap", "load_inputs", "fit", "plots", "upload", "done"} <= set(phases), phases
    assert any(i.event.type is RunEventType.COMPLETE for i in persisted)
    fractions = [i.payload["fraction"] for i in live if i.type is RunEventType.PROGRESS]
    assert fractions, "no live progress frame reached the client"
    assert all(isinstance(f, float | int) and 0 <= f <= 1 for f in fractions)
    ids = [i.id for i in persisted]
    assert ids == sorted(set(ids)), "persisted event ids must be unique and increasing"
    assert [e.id for e in api.runs.events(fit.run.id)] == ids  # JSON page == the stream
    # every state change was announced with a fresh snapshot
    assert snapshots(stream)[-1].state.value in ("finalizing", "succeeded")
    assert fit.run.executor == "local-process" and fit.run.attempt == 1


def test_event_stream_resumes_after_last_event_id(api: Client, fit: FitResult) -> None:
    ids = [i.id for i in fit.stream if isinstance(i, RunEventFrame)]
    resumed = list(api.runs.stream_events(fit.run.id, last_event_id=ids[2]))
    assert [i.id for i in resumed if isinstance(i, RunEventFrame)] == ids[3:]
    assert isinstance(resumed[0], RunSnapshot) and isinstance(resumed[-1], RunEnd)


# ============================================================================ finalization


def test_finalization_signs_a_manifest_bound_to_the_bundle_generations(
    api: Client, stack: Stack, fit: FitResult
) -> None:
    mv = fit.version
    assert mv.status == "ready" and mv.run_id == fit.run.id
    manifest = mv.manifest
    assert isinstance(manifest, BundleManifest)
    assert manifest.signature is not None and manifest.signature.alg == "KMS-EC-P256-SHA256"
    if not stack.signing_key.is_file():
        pytest.fail(f"no bundle signing key at {stack.signing_key}")
    signer = BundleSigner.from_file(stack.signing_key, create=False)
    assert signer.verify(manifest), "the stored manifest's signature does not verify"
    forged = manifest.model_copy(update={"spec_hash": "sha256:" + "0" * 64})
    assert not signer.verify(forged), "a changed manifest must not verify"
    assert (manifest.tenant_id, manifest.project_id) == ("default", "default")
    assert (manifest.model_id, manifest.model_version) == (mv.model_id, mv.version)

    buckets = stack.local.buckets
    prefix = bundle_prefix("default", mv.model_id, mv.version)
    names = {a.name for a in mv.artifacts}
    assert set(manifest.objects) == names
    assert {"bundle.zarr.zip", "bundle.manifest.json", "plots.json", "diagnostics.json"} <= names
    for name, obj in manifest.objects.items():
        # copied server-side into the bundles bucket (exec principal: no access) ...
        assert obj.bucket == buckets.bundles and obj.key == prefix + name
        # ... and every URL is minted for exactly the signed generation, size and CRC32C
        link = api.models.artifact(mv.model_id, mv.version, name)
        assert (link.generation, link.size, link.crc32c) == (obj.generation, obj.size, obj.crc32c)
    key = manifest.objects["bundle.zarr.zip"].key
    path = f"/storage/v1/b/{buckets.bundles}/o/{key.replace('/', '%2F')}"
    assert shim_request(stack, "exec", "GET", path).status_code == 403
    assert shim_request(stack, "control", "GET", path).status_code == 200


def test_bundle_reads_back_and_matches_the_corpus_expectations(
    bundle: Bundle, e33_data: E33Data, registry: Registry
) -> None:
    ctx = FixtureContext("e33", SEED, SIZE, registry, e33_data.side)
    check_fit(bundle, e33_data.data, ctx=ctx)


def test_lineage_pins_data_side_tables_and_runtime(
    api: Client, fit: FitResult, e33_data: E33Data
) -> None:
    lineage = fit.version.lineage
    main = api.datasets.find("e2e_eu_media_daily")
    assert main is not None
    data_version = api.datasets.version(main.id, main.latest_version or 1)
    assert lineage.dataset_sha == data_version.sha256
    assert set(lineage.side_tables) == {"fx", "holidays"}
    assert lineage.plugin is not None and lineage.plugin.startswith("mmm.bayesian@1.")
    assert (lineage.executor, lineage.engine, lineage.seed) == ("local-process", "pymc", 20261014)
    assert lineage.spec_hash == fit.version.spec_hash and lineage.catalog_digest


# ================================================================ inspect: scorecard & plots


def test_scorecard_diagnostics_and_metrics(fit: FitResult) -> None:
    mv = fit.version
    assert mv.scorecard is not None and mv.scorecard.items
    # the runner's scorecard (v0.4): one item per catalog check (mapi_spec.scorecard)
    checks = {i.check for i in mv.scorecard.items}
    assert {"convergence.rhat", "convergence.ess", "convergence.divergences"} <= checks, checks
    assert mv.scorecard.overall in ("pass", "warn", "fail")
    assert all(i.message_plain for i in mv.scorecard.items)
    assert mv.diagnostics and {d.code for d in mv.diagnostics} >= {"RHAT", "DIVERGENCES"}
    assert {"coverage_90", "mape", "elpd_loo"} <= set(mv.metrics)
    assert mv.capabilities is not None and "forecast" in mv.capabilities.score_modes


def test_plots_are_sanitized_and_their_data_resolves(api: Client, fit: FitResult) -> None:
    mv = fit.version
    listed = list(api.plots.list(mv.model_id, mv.version))
    ids = {p.id for p in listed}
    assert {"core.ppc_fan", "mmm.decomposition", "core.convergence", "mmm.response_curves"} <= ids
    artifacts = {a.name for a in mv.artifacts}
    for item in listed:
        plot = api.plots.get(mv.model_id, mv.version, item.id)
        assert plot.kind == "vega-lite" and plot.spec and plot.caption_plain
        assert api.plots.validate(plot).valid, f"{plot.id} does not pass the sanitizer again"
        for name, ref in plot.datasets.items():
            if ref.kind == "data_ref":
                assert ref.path in artifacts, f"{plot.id}: dataset {name} -> {ref.path}"
    body = api.models.download(mv.model_id, mv.version, "plots.json")
    assert isinstance(body, bytes)
    raw = json.loads(body)
    assert {p["id"] for p in raw} == ids


def test_ui_views_redirect_to_signed_urls(
    api: Client, stack: Stack, stack_credentials: StackAuth, fit: FitResult
) -> None:
    mv = fit.version
    base = stack.local.api_url.rstrip("/")
    path = f"{base}/v1/models/{mv.model_id}/versions/{mv.version}/ui/tree_index.json"
    with httpx.Client(timeout=30) as http:
        hop = http.get(path, follow_redirects=False, headers=stack_credentials.headers())
        assert hop.status_code == 307
        location = hop.headers["location"]
        assert location.startswith(stack.local.gcs_endpoint.rstrip("/"))
        index = TreeIndexView.model_validate_json(http.get(location).content)
        groups = set(index.groups)
        assert {"/model/posterior", "/model/observed_data", "/derived/contributions"} <= groups
        # v0.5: mmm-framework owns scaling and carry-over; the pipeline converts currencies,
        # aggregates to weeks and adds the holidays
        assert {"/preprocess/to_eur", "/preprocess/weekly", "/preprocess/hols"} <= groups
        posterior = index.groups["/model/posterior"]
        assert {"chain", "draw"} <= set(posterior.dims), posterior.dims
        summaries = http.get(
            path.replace("tree_index.json", "summaries.json"),
            follow_redirects=True,  # httpx drops Authorization on the hop to the store
            headers=stack_credentials.headers(),
        )
        assert summaries.status_code == 200 and summaries.json()
        pre = http.get(
            path.replace("tree_index.json", "preprocess.json"),
            follow_redirects=True,
            headers=stack_credentials.headers(),
        )
        assert pre.status_code == 200, pre.text
        steps = {s.id: s for s in PreprocessView.model_validate_json(pre.content).steps}
        assert list(steps) == ["to_eur", "weekly", "hols"]
        assert not any(s.stateful for s in steps.values())  # nothing is learned before the model
        missing = http.get(
            path.replace("tree_index.json", "nope.json"),
            follow_redirects=False,
            headers=stack_credentials.headers(),
        )
        assert missing.status_code == 404


# ================================================================================== score


def _score_requests(refs: dict[str, str], model_ref: str) -> list[tuple[str, dict[str, Any]]]:
    """The corpus' E33 score requests against the registered model and uploaded payloads."""
    path = Path(e33.__file__).parent / "score_requests.yaml"
    out: list[tuple[str, dict[str, Any]]] = []
    for entry in yaml.safe_load(path.read_text(encoding="utf-8")):
        req: dict[str, Any] = {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": model_ref}
        req.update(entry["request"])
        req["data"] = refs[entry["payload"]]
        for key in ("data", "scenario"):
            if req.get(key) in SCENARIO_REFS:
                req[key] = refs[SCENARIO_REFS[req[key]]]
        if entry.get("scenario"):
            req["scenario"] = refs[entry["scenario"]]
        tables = req.get("side_tables") or {}
        req["side_tables"] = {name: refs["dataset://growth/fx_daily@latest"] for name in tables}
        out.append((str(entry["name"]), req))
    return out


EXPECTED_SCORE_PLOTS = {
    "forecast_8w": {"core.ppc_fan"},
    "new_market": {"core.ppc_fan"},
    "weekly_eur_plan": {"core.ppc_fan"},
    "counterfactual_tv": {"core.counterfactual"},
    "counterfactual_tv_plan": {"core.counterfactual"},
}


def test_score_horizon_new_market_weekly_plan_and_counterfactuals(
    api: Client, fit: FitResult, e33_data: E33Data, bundle: Bundle, registry: Registry
) -> None:
    mv = fit.version
    ctx = FixtureContext("e33", SEED, SIZE, registry, e33_data.side)
    requests = _score_requests(e33_data.refs, mv.ref)
    assert [n for n, _ in requests] == list(EXPECTED_SCORE_PLOTS)
    for name, request in requests:
        result = api.models.score(mv.model_id, mv.version, request, timeout=900)
        assert result.run.state.value == "succeeded", (name, result.run.problem)
        assert result.tree is not None, f"{name}: no result tree"
        outputs = {o.name for o in result.outputs}
        assert {"score.zarr.zip", "result.json", "ui/summary.arrow", "plots.json"} <= outputs
        # the corpus' own expectations: horizon, EUR units, UNSEEN_LEVELS for FR, the plan
        # entering at 'weekly' equals the raw path, CRN counterfactual deltas
        check_score(name, result.tree, bundle, e33_data.data, ctx=ctx)
        ctx.results[name] = result.tree
        plots = {p.id for p in api.runs.plots(result.run.id)}
        assert EXPECTED_SCORE_PLOTS[name] <= plots, (name, plots)


# =============================================================================== security


def _attempt_rows(stack: Stack, run_id: str) -> list[tuple[int, str, int]]:
    """``(attempt, nonce, violations)`` of a run, read straight from the database."""
    with psycopg.connect(stack.database_url) as conn:
        rows = conn.execute(
            "SELECT attempt, nonce, violations FROM run_attempts WHERE run_id = %s "
            "ORDER BY attempt",
            (run_id,),
        ).fetchall()
    return [(int(a), str(n), int(v)) for a, n, v in rows]


def test_bootstrap_object_is_gone_once_the_supervisor_read_it(stack: Stack, fit: FitResult) -> None:
    rows = _attempt_rows(stack, fit.run.id)
    assert rows, "the run has no intent row"
    bootstrap = stack.local.buckets.bootstrap
    for _, nonce, _ in rows:
        path = f"/storage/v1/b/{bootstrap}/o/{nonce}"
        # the exec principal (the strongest credential a sandbox could hold) gets nothing ...
        assert shim_request(stack, "exec", "GET", path, params={"alt": "media"}).status_code == 404
        # ... because the object does not exist any more, not because of permissions
        assert shim_request(stack, "control", "GET", path).status_code == 404
    listing = shim_request(stack, "exec", "GET", f"/storage/v1/b/{bootstrap}/o")
    assert listing.status_code == 403, "the exec principal must not list the bootstrap bucket"


def _forged(run: Run, *, key: bytes | None, attempt: int | None = None) -> dict[str, Any]:
    event = RunEvent(
        run_id=run.id,
        attempt=attempt or run.attempt,
        seq=10**9 + secrets.randbelow(10**6),
        type=RunEventType.ERROR,
        ts=run.created_at,
        payload={"code": "USER_CODE_ERROR", "failure_class": "user", "message": "forged"},
    )
    if key is not None:
        event = sign_event(event, key)
    return {"mapi_event": event.model_dump(mode="json")}


def test_forged_run_events_are_refused(
    api: Client, stack: Stack, stack_credentials: StackAuth, fit: FitResult
) -> None:
    run = fit.run
    headers = stack_credentials.internal_headers()
    url = stack.local.api_url.rstrip("/") + "/internal/events"
    before = api.runs.get(run.id)
    events_before = [e.id for e in api.runs.events(run.id)]
    violations_before = sum(v for _, _, v in _attempt_rows(stack, run.id))
    with httpx.Client(timeout=30, headers=headers) as http:
        wrong_key = http.post(url, json=_forged(run, key=secrets.token_bytes(32)))
        unsigned = http.post(url, json=_forged(run, key=None))
        unknown = http.post(url, json=_forged(run, key=secrets.token_bytes(32), attempt=99))
        garbage = http.post(url, json={"mapi_event": {"run_id": run.id}})
    assert wrong_key.status_code == 200 and wrong_key.json()["result"] == "bad_hmac"
    assert unsigned.status_code == 200 and unsigned.json()["result"] == "bad_hmac"
    assert unknown.status_code == 200 and unknown.json()["result"] == "unknown_attempt"
    assert garbage.status_code == 422
    after = api.runs.get(run.id)
    assert after.state == before.state and after.problem == before.problem
    assert [e.id for e in api.runs.events(run.id)] == events_before
    assert sum(v for _, _, v in _attempt_rows(stack, run.id)) == violations_before + 2
