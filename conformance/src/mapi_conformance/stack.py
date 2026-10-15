"""The corpus through a running stack: spec → **ingest** → fit → score, as a user does (plan §13).

:mod:`mapi_conformance.runner` drives fixtures through the in-process executor, which skips
the control plane. This mode drives the same fixtures through the API of a running stack
(``make dev``: API server, worker, GCS shim, local-process executor), so every run goes through
the sandboxed ingest, the dispatcher, the runner's supervisor and worker, FINALIZING (signed
manifest, server-side copy) and signed downloads:

1. **ingest** - the fixture's data (with its raw schema), side tables and every score payload
   are uploaded as dataset versions (signed PUT → sandboxed ingest run);
2. **validate** - the spec, pointed at the uploads and renamed per session, through
   ``POST /v1/specs:validate``;
3. **fit** - the spec is created and fitted; the run must reach ``succeeded`` and register a
   model version;
4. **score** - every ``score_requests.yaml`` entry against the registered model, checked like
   the in-process runner checks them, plus the fixture's ``check_score`` hook; an expected
   typed error may come from the failed run or from the API refusing the request up front;
5. **evaluate** - the fit-time holdout metrics (``evaluate_then_refit``) are on the version;
6. **plots** - the spec's ``outputs.plots`` were rendered and stored;
7. **expectations** - the bundle, downloaded through signed URLs and read with every hash
   verified, passes the fixture's ``check_fit`` hook.

Use it with ``python -m mapi_conformance run --stack http://127.0.0.1:8000`` or
:func:`run_fixture_on_stack`. Names are prefixed and suffixed per session, so runs never
collide with each other or with user data.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_spec import ModelSpec
from mapi_spec.errors import MapiError
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.spec import ScorePolicy

from mapi_conformance.runner import (
    CorpusReport,
    Fixture,
    FixtureContext,
    FixtureReport,
    ScoreCase,
    Size,
    _call_hook,
    _check_expect,
    _skip,
    _Step,
    load_fixture,
)

__all__ = [
    "STACK_STEPS",
    "StackAuth",
    "StackRefs",
    "dataset_name",
    "run_corpus_on_stack",
    "run_fixture_on_stack",
    "stack_auth",
    "stack_request",
    "stack_spec_yaml",
]

STACK_STEPS = ("ingest", "validate", "fit", "score", "evaluate", "plots", "expectations")
DEFAULT_DEV_ACCOUNT = "admin@dev.local"
DEFAULT_PROJECT = "default/default"


@dataclass(frozen=True, slots=True)
class StackAuth:
    """How to call a running stack. All ``None`` for a single-tenant stack (no credentials).

    ``make dev`` runs the servers in the ``dev`` profile, where every request needs a caller:
    ``token`` is ``MAPI_TOKEN`` or an ID token from the stack's development IdP, ``project``
    the ``X-Mapi-Project`` to act in, and ``internal_token`` the bearer of
    ``POST /internal/events`` (``MAPI_INTERNAL_TOKEN``, or the stack's
    ``$MAPI_DATA_DIR/dev/internal-token``).
    """

    token: str | None = None
    project: str | None = None
    internal_token: str | None = None

    def headers(self) -> dict[str, str]:
        """Headers for raw HTTP calls to the stack's project-scoped ``/v1`` routes."""
        from mapi_spec.tenancy import PROJECT_HEADER

        out: dict[str, str] = {}
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        if self.project:
            out[PROJECT_HEADER] = self.project
        return out

    def internal_headers(self) -> dict[str, str]:
        """Headers for ``POST /internal/events``."""
        return {"Authorization": f"Bearer {self.internal_token}"} if self.internal_token else {}


def stack_auth(
    api_url: str, *, data_dir: Path | None = None, environ: Mapping[str, str] | None = None
) -> StackAuth:
    """Work out how to call the stack at ``api_url`` (see :class:`StackAuth`).

    Environment: ``MAPI_TOKEN`` (wins), ``MAPI_DEV_ACCOUNT`` (default ``admin@dev.local``),
    ``MAPI_PROJECT`` (default ``default/default``), ``MAPI_INTERNAL_TOKEN``.
    """
    import os

    import httpx
    from mapi_client.oidc import dev_idp_login

    env = os.environ if environ is None else environ
    url = api_url.rstrip("/")
    internal = env.get("MAPI_INTERNAL_TOKEN", "").strip() or None
    if internal is None and data_dir is not None:
        token_file = data_dir / "dev" / "internal-token"
        if token_file.exists():
            internal = token_file.read_text(encoding="utf-8").strip() or None
    try:
        status = httpx.get(f"{url}/v1/me", timeout=10).status_code
    except httpx.TransportError:
        return StackAuth(internal_token=internal)  # callers report the unreachable API
    if status != 401:
        return StackAuth(internal_token=internal)
    token = env.get("MAPI_TOKEN", "").strip() or dev_idp_login(
        url, env.get("MAPI_DEV_ACCOUNT", "").strip() or DEFAULT_DEV_ACCOUNT
    )
    project = env.get("MAPI_PROJECT", "").strip() or DEFAULT_PROJECT
    return StackAuth(token=token, project=project, internal_token=internal)


_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class StackRefs:
    """``dataset://`` refs of the uploads: the data, side tables by name, payloads by key."""

    data: str = ""
    side_tables: dict[str, str] = field(default_factory=dict)
    payloads: dict[str, str] = field(default_factory=dict)


def dataset_name(prefix: str, fixture_id: str, token: str, part: str) -> str:
    """A dataset name (a slug) for one upload of one fixture in one session."""
    raw = "-".join(
        _UNSAFE.sub("-", p.lower()).strip("-") for p in (prefix, fixture_id, token, part)
    )
    return raw[:120].strip("-")


def _payload_key(name: str, drop_targets: bool) -> str:
    return name if drop_targets else f"{name}+targets"


def stack_spec_yaml(fx: Fixture, size: Size, refs: StackRefs, *, name: str) -> str:
    """The fixture's spec (``tiny`` overrides applied) pointed at the uploaded datasets, in
    project ``default`` under ``name`` (JSON, which is YAML)."""
    doc = fx.spec(size).model_dump(mode="json", by_alias=True, exclude_unset=True)
    doc["metadata"] = {**doc.get("metadata", {}), "name": name, "project": "default"}
    data = doc["data"]
    data["dataset"] = refs.data
    for table, entry in (data.get("side_tables") or {}).items():
        entry["ref"] = refs.side_tables[table]
    return json.dumps(doc, indent=1)


def _supplied(spec: ModelSpec) -> list[str]:
    return [n for n, st in spec.data.side_tables.items() if st.score_policy is ScorePolicy.SUPPLIED]


def stack_request(
    case: ScoreCase, spec: ModelSpec, refs: StackRefs, model_ref: str
) -> dict[str, Any]:
    """A score request of ``case`` against ``model_ref`` with the uploaded payloads and side
    tables (the fixture refs in the request are replaced; ``side_tables: false`` drops them)."""
    req = copy.deepcopy(case.request)
    req["model"] = model_ref
    if case.payload != "none":
        req["data"] = refs.payloads[_payload_key(case.payload, case.drop_targets)]
    if case.scenario is not None:
        req["scenario"] = refs.payloads[_payload_key(case.scenario, case.drop_targets)]
    if not case.side_tables:
        req.pop("side_tables", None)
        return req
    tables = dict(req.get("side_tables") or {})
    for name in _supplied(spec):
        tables.setdefault(name, "")
    if tables:
        req["side_tables"] = {name: refs.side_tables[name] for name in tables}
    return req


# ================================================================================ runner


def _upload(api: Any, ds: xr.Dataset, name: str, timeout: float) -> str:
    version = api.datasets.upload(ds, dataset=name, timeout=timeout)
    if version.status.value != "ready":
        raise RuntimeError(f"{name}: ingest ended {version.status.value}")
    return str(version.ref)


def _bundle(api: Any, mv: Any, registry: Registry) -> Bundle:
    """The registered bundle, downloaded through signed URLs (size and CRC32C checked) and
    read with every file hash verified against the SDK manifest."""
    with tempfile.TemporaryDirectory(prefix="mapi-corpus-") as tmp:
        root = Path(tmp)
        for obj in mv.artifacts:
            if obj.name.startswith("ui/"):
                continue
            local = "manifest.json" if obj.name == "bundle.manifest.json" else obj.name
            dest = root / local
            dest.parent.mkdir(parents=True, exist_ok=True)
            api.models.download(mv.model_id, mv.version, obj.name, dest)
        return Bundle.read(root, registry, verify=True)


def run_fixture_on_stack(
    fixture_id: str,
    api: Any,
    *,
    size: Size = "tiny",
    seed: int = 0,
    prefix: str = "corpus",
    token: str | None = None,
    timeout: float = 1800.0,
    log: Callable[[str], None] | None = None,
) -> FixtureReport:
    """Run one fixture through the stack behind ``api`` (a :class:`mapi_client.Client`);
    never raises for fixture failures (see the report)."""
    report = FixtureReport(fixture=fixture_id, size=size)
    t0 = time.perf_counter()
    try:
        _run(report, fixture_id, api, size, seed, prefix, token or uuid.uuid4().hex[:8], timeout,
             log or (lambda _m: None))  # fmt: skip
    finally:
        report.seconds = time.perf_counter() - t0
    return report


def _run(
    report: FixtureReport,
    fixture_id: str,
    api: Any,
    size: Size,
    seed: int,
    prefix: str,
    token: str,
    timeout: float,
    say: Callable[[str], None],
) -> None:
    fx = load_fixture(fixture_id)
    data, tables = fx.generate(seed, size)
    spec = fx.spec(size)
    schema = fx.raw_schema(data)
    registry = default_registry().copy()
    hook = fx.hook("registry")
    if callable(hook):
        registry = hook(registry)
    report.model = str(spec.model.use)
    ctx = FixtureContext(fixture_id, seed, size, registry, dict(tables))
    name = dataset_name(prefix, fixture_id, token, "model")

    refs = StackRefs()
    with _Step(report, "ingest") as st:
        upload = data.assign_attrs({DATASET_SCHEMA_ATTR: schema.model_dump_json()})
        refs.data = _upload(api, upload, dataset_name(prefix, fixture_id, token, "data"), timeout)
        for table, ds in sorted(tables.items()):
            refs.side_tables[table] = _upload(
                api, ds, dataset_name(prefix, fixture_id, token, table), timeout
            )
        for case in fx.cases:
            for payload in (case.payload, case.scenario):
                if payload is None or payload == "none":
                    continue
                key = _payload_key(payload, case.drop_targets)
                if key in refs.payloads:
                    continue
                one = fx.payload(
                    dataclasses.replace(case, payload=payload), data, spec, schema, seed, size
                )
                assert one is not None
                refs.payloads[key] = _upload(
                    api, one, dataset_name(prefix, fixture_id, token, key.replace("+", "-")),
                    timeout,
                )  # fmt: skip
        st.detail = f"{1 + len(refs.side_tables) + len(refs.payloads)} dataset versions"
    say(f"[{fixture_id}] ingest {'ok' if report.steps[-1].ok else 'FAILED'}")
    if not report.steps[-1].ok:
        for step in STACK_STEPS[1:]:
            _skip(report, step, "ingest failed")
        return

    text = stack_spec_yaml(fx, size, refs, name=name)
    with _Step(report, "validate") as st:
        verdict = api.specs.validate(text)
        if not verdict.valid:
            raise AssertionError(
                "; ".join(f"{i.code.value} at {i.pointer}: {i.message}" for i in verdict.issues)
            )
        st.detail = "valid"

    mv: Any = None
    with _Step(report, "fit") as st:
        doc = api.specs.create(text, message=f"conformance corpus: {fixture_id} ({size})")
        run = api.runs.fit(doc.id, doc.latest_revision, labels={"suite": "corpus"})
        final = api.runs.wait(run.id, timeout=timeout)
        if final.state.value != "succeeded":
            problem = final.problem
            why = f"{problem.code.value}: {problem.detail}" if problem is not None else ""
            raise RuntimeError(f"fit run {final.id} ended {final.state.value} {why}".strip())
        model = next(m for m in api.models.list() if m.name == name)
        mv = api.models.version(model.id, model.latest_version or 1)
        report.model = str(mv.lineage.plugin or report.model)
        report.metrics.update(mv.metrics)
        st.detail = f"run {final.id}; {mv.ref}"
    say(f"[{fixture_id}] fit {'ok' if report.steps[-1].ok else 'FAILED'}")
    if mv is None:
        for step in STACK_STEPS[3:]:
            _skip(report, step, "fit failed")
        return

    bundle: Bundle | None = None
    bundle_error = ""
    try:
        bundle = _bundle(api, mv, registry)
    except Exception as exc:  # reported by the expectations step below
        bundle_error = traceback.format_exception_only(type(exc), exc)[-1].strip()
    _score(report, fx, api, spec, refs, mv, bundle, data, ctx, timeout)

    with _Step(report, "evaluate") as st:
        wanted = [m for m in spec.evaluate.metrics if not m.startswith("elpd_")]
        if spec.model.fit.mode.value == "evaluate_then_refit" and wanted:
            missing = [m for m in wanted if m not in mv.metrics]
            if missing:
                raise AssertionError(f"holdout metrics {missing} are not on {mv.ref}")
            st.detail = ", ".join(f"{m}={mv.metrics[m]:.4g}" for m in wanted)
        else:
            st.skipped, st.detail = True, "no holdout evaluation in this spec"

    ids = [p.id for p in spec.outputs.plots]
    if ids:
        with _Step(report, "plots") as st:
            stored = {p.id for p in api.plots.list(mv.model_id, mv.version)}
            missing = [i for i in ids if i not in stored]
            st.passed, st.total = len(ids) - len(missing), len(ids)
            st.ok = not missing
            st.detail = f"not stored: {missing}" if missing else f"stored {sorted(ids)}"
    else:
        _skip(report, "plots", "the spec lists no plots")

    with _Step(report, "expectations") as st:
        if bundle is None:
            raise AssertionError(f"the bundle could not be read back: {bundle_error}")
        hook = fx.hook("check_fit")
        if callable(hook):
            _call_hook(hook, bundle, data, ctx=ctx)
        st.detail = "bundle verified" + ("; check_fit" if callable(hook) else "")


def _score(
    report: FixtureReport,
    fx: Fixture,
    api: Any,
    spec: ModelSpec,
    refs: StackRefs,
    mv: Any,
    bundle: Bundle | None,
    data: xr.Dataset,
    ctx: FixtureContext,
    timeout: float,
) -> None:
    if not fx.cases:
        _skip(report, "score", "no score_requests.yaml")
        return
    with _Step(report, "score") as st:
        failures: list[str] = []
        passed = 0
        for case in fx.cases:
            try:
                request = stack_request(case, spec, refs, str(mv.ref))
                problems: list[str] = []
                try:
                    res = api.models.score(
                        mv.model_id, mv.version, request, timeout=timeout, raise_on_failure=False
                    )
                except MapiError as exc:
                    # the control plane refused the request before any run (a typed 4xx)
                    if case.expect_error is None:
                        raise
                    if exc.code.value != case.expect_error:
                        problems.append(f"expected {case.expect_error}, got {exc.code.value}")
                    failures += [f"{case.name}: {'; '.join(problems)}"] if problems else []
                    passed += 0 if problems else 1
                    continue
                run = res.run
                code = run.problem.code.value if run.problem is not None else None
                if case.expect_error is not None:
                    if run.state.value == "succeeded":
                        problems.append(f"expected {case.expect_error} but the run succeeded")
                    elif code != case.expect_error:
                        problems.append(f"expected {case.expect_error}, got {code}")
                elif run.state.value != "succeeded":
                    detail = run.problem.detail if run.problem is not None else ""
                    problems.append(f"run {run.id} ended {run.state.value}: {code} {detail}")
                elif res.tree is None:
                    problems.append("no result tree was downloaded")
                else:
                    problems += _check_expect(case, res.tree)
                    hook = fx.hook("check_score")
                    if callable(hook) and not problems and bundle is not None:
                        _call_hook(hook, case.name, res.tree, bundle, data, ctx=ctx)
                    ctx.results[case.name] = res.tree
                if problems:
                    failures.append(f"{case.name}: {'; '.join(problems)}")
                else:
                    passed += 1
            except Exception as exc:  # one broken case never hides the others
                last = traceback.format_exception_only(type(exc), exc)[-1].strip()
                failures.append(f"{case.name}: {last}")
        st.passed, st.total = passed, len(fx.cases)
        st.ok = not failures
        st.detail = " | ".join(failures)[:4000]


def run_corpus_on_stack(
    api: Any,
    fixtures: Iterable[str],
    *,
    size: Size = "tiny",
    seed: int = 0,
    prefix: str = "corpus",
    timeout: float = 1800.0,
    log: Callable[[str], None] | None = None,
) -> CorpusReport:
    """Run ``fixtures`` through the stack; one session token names every upload and model."""
    token = uuid.uuid4().hex[:8]
    reports = [
        run_fixture_on_stack(
            f, api, size=size, seed=seed, prefix=prefix, token=token, timeout=timeout, log=log
        )
        for f in fixtures
    ]
    return CorpusReport(reports)
