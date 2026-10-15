"""Settings, the in-memory object store, the bundle signer, the docker executor's container
options and the state-machine service (exhaustively against ``mapi_spec.run``)."""

from __future__ import annotations

import itertools
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import libpq_url, sqlalchemy_url
from mapi_server.db import models as m
from mapi_server.executors.docker import LABEL_NONCE, DockerExecutor
from mapi_server.services.runs import Scope, apply_transition, create_run
from mapi_server.services.signing import SIGNATURE_ALG, BundleSigner
from mapi_server.settings import Settings, local_image_digest
from mapi_server.storage import MemoryObjectStore, list_all
from mapi_spec._types import Duration
from mapi_spec.executor import PLUGIN_LABEL, ExecState, Isolation, RunKind, RunSubmit
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    Actor,
    BundleManifest,
    IllegalTransition,
    RunState,
    RunTrigger,
    sign_bundle_manifest,
    transition,
)
from mapi_spec.storage import (
    AccessDenied,
    NotFound,
    ObjectStore,
    PreconditionFailed,
    StorageError,
)
from sqlalchemy import select

# ---------------------------------------------------------------------------- settings


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")
    monkeypatch.setenv("MAPI_RUNNER_COMMAND", "python -m mapi_runner --flag 'a b'")
    monkeypatch.setenv("MAPI_ENV", "dev")
    monkeypatch.setenv("MAPI_EXECUTOR", "docker-gvisor")
    s = Settings()
    assert s.database_url == "postgresql://u@h/db"
    assert s.runner_command == ["python", "-m", "mapi_runner", "--flag", "a b"]
    assert s.buckets.runs == "mapi-dev-runs" and s.executor == "docker-gvisor"
    assert s.bootstrap_url_template() == (
        "http://127.0.0.1:4443/storage/v1/b/mapi-dev-bootstrap/o/{nonce}"
    )
    with pytest.raises(ValueError):
        Settings(web_origin="http://x", plots_origin="http://x/")
    with pytest.raises(ValueError):
        Settings(env="Bad_Env")
    lock = tmp_path / "uv.lock"
    lock.write_text("lock")
    assert Settings(uv_lock_path=lock).resolve_image_digest() == local_image_digest(lock)
    assert Settings(image_digest="sha256:" + "1" * 64).resolve_image_digest().endswith("1" * 64)


def test_database_url_forms() -> None:
    assert sqlalchemy_url("postgresql://a/b") == "postgresql+psycopg://a/b"
    assert sqlalchemy_url("postgres://a/b") == "postgresql+psycopg://a/b"
    assert libpq_url("postgresql+psycopg://a/b?host=/tmp") == "postgresql://a/b?host=/tmp"
    with pytest.raises(ValueError):
        sqlalchemy_url("mysql://a/b")
    with pytest.raises(ValueError):
        libpq_url("sqlite:///x")


# ------------------------------------------------------------------------ memory store


async def test_memory_store_gcs_semantics() -> None:
    store = MemoryObjectStore(buckets=["bkt", "lck-bkt"], retention_locked=["lck-bkt"])
    assert isinstance(store, ObjectStore)
    a = await store.put("bkt", "x/1", b"hello", content_type="text/plain")
    assert a.size == 5 and a.crc32c and a.md5_hash and a.generation >= 1
    with pytest.raises(PreconditionFailed):
        await store.put("bkt", "x/1", b"again", if_generation_match=0)
    b = await store.put("bkt", "x/1", b"world", if_generation_match=a.generation)
    assert b.generation > a.generation
    assert await store.get("bkt", "x/1", generation=a.generation) == b"hello"
    assert await store.get("bkt", "x/1", byte_range=(1, 3)) == b"orl"
    with pytest.raises(StorageError) as big:
        await store.get("bkt", "x/1", max_bytes=2)
    assert big.value.code == "PAYLOAD_TOO_LARGE"
    c = await store.copy(
        "bkt", "x/1", "bkt", "y/1", source_generation=a.generation, if_generation_match=0
    )
    assert c.same_content(a) and c.generation != a.generation
    for i in range(5):
        await store.put("bkt", f"z/{i}", b"")
    page = await store.list("bkt", prefix="z/", max_results=2)
    assert [o.name for o in page.items] == ["z/0", "z/1"] and page.next_page_token
    assert len(await list_all(store, "bkt", "z/")) == 5
    dirs = await store.list("bkt", delimiter="/")
    assert set(dirs.prefixes) == {"x/", "y/", "z/"}
    await store.delete("bkt", "y/1")
    with pytest.raises(NotFound):
        await store.stat("bkt", "y/1")
    with pytest.raises(NotFound):
        await store.put("missing-bucket", "k", b"")
    await store.put("lck-bkt", "k", b"v")
    with pytest.raises(AccessDenied):
        await store.delete("lck-bkt", "k")
    with pytest.raises(ValueError):
        await store.put("bkt", "../escape", b"")


async def test_memory_store_signed_urls() -> None:
    store = MemoryObjectStore(base_url="http://s", buckets=["bkt"])
    meta = await store.put("bkt", "k/obj", b"data")
    get = await store.sign_get("bkt", "k/obj", expires_in=timedelta(minutes=5))
    assert get.generation == meta.generation and get.url.startswith("http://s/download/")
    q = dict(p.split("=", 1) for p in get.url.split("?", 1)[1].split("&"))
    assert store.verify_get(
        "bkt", "k/obj", meta.generation, int(q["X-Goog-Expires"]), q["X-Goog-Signature"]
    )
    assert not store.verify_get(
        "bkt", "k/other", meta.generation, int(q["X-Goog-Expires"]), q["X-Goog-Signature"]
    )
    put = await store.sign_put("bkt", "k/new", expires_in=timedelta(minutes=5), max_bytes=10)
    assert put.headers["x-goog-if-generation-match"] == "0" and put.max_bytes == 10
    policy = await store.post_policy("bkt", "k/", expires_in=timedelta(minutes=5), max_bytes=99)
    doc = store.verify_policy(policy.fields["policy"], policy.fields["x-goog-signature"])
    assert doc is not None and doc.prefix == "k/" and doc.max_bytes == 99
    assert store.verify_policy(policy.fields["policy"], "0" * 64) is None
    with pytest.raises(ValueError):
        await store.sign_get("bkt", "k/obj", expires_in=timedelta(days=8))


# ------------------------------------------------------------------------------ signer


def _manifest() -> BundleManifest:
    example = BundleManifest.model_json_schema()["examples"][0]
    example.pop("signature")
    return BundleManifest.model_validate(example)


def test_bundle_signer_signs_and_verifies(tmp_path: Path) -> None:
    signer = BundleSigner.from_file(tmp_path / "keys" / "k.pem")
    assert stat.S_IMODE((tmp_path / "keys" / "k.pem").stat().st_mode) == 0o600
    signed = signer.sign(_manifest())
    assert signed.signature is not None and signed.signature.alg == SIGNATURE_ALG
    assert signer.verify(signed)
    reloaded = BundleSigner.from_file(tmp_path / "keys" / "k.pem")
    assert reloaded.key_id == signer.key_id and reloaded.verify(signed)
    tampered = signed.model_copy(update={"model_version": 2})
    assert not signer.verify(tampered)
    assert not BundleSigner.generate().verify(signed)  # unknown key id
    assert not signer.verify(_manifest())  # unsigned
    hs = sign_bundle_manifest(_manifest(), b"k" * 32, "hs")
    assert not signer.verify(hs)  # HS256 is not accepted for bundles minted by this server
    assert "PUBLIC KEY" in signer.public_pem()
    with pytest.raises(FileNotFoundError):
        BundleSigner.from_file(tmp_path / "nope.pem", create=False)


# ----------------------------------------------------------------------------- docker


class _FakeContainer:
    def __init__(self, labels: dict[str, str], state: dict[str, Any]) -> None:
        self.id = "c0ffee"
        self.labels = labels
        self.attrs = {"State": state}

    def reload(self) -> None:
        pass

    def logs(self, **_: Any) -> list[bytes]:
        return []

    stop_delay = 0.0

    def stop(self, timeout: int) -> None:
        import time

        time.sleep(self.stop_delay)  # the real SDK blocks up to `timeout` seconds
        self.attrs["State"] = {"Status": "exited", "ExitCode": 143}


class _FakeContainers:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.container: _FakeContainer | None = None

    def run(self, **opts: Any) -> _FakeContainer:
        self.created.append(opts)
        self.container = _FakeContainer(opts["labels"], {"Status": "running"})
        return self.container

    def list(self, all: bool, filters: dict[str, str]) -> list[_FakeContainer]:
        c = self.container
        return [c] if c and filters["label"] == f"{LABEL_NONCE}={c.labels[LABEL_NONCE]}" else []

    def get(self, cid: str) -> _FakeContainer:
        assert self.container is not None
        return self.container


class _FakeDocker:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


TEMPLATE = "http://127.0.0.1:4443/storage/v1/b/mapi-local-bootstrap/o/{nonce}"


def test_docker_container_options_lock_the_sandbox_down() -> None:
    ex = DockerExecutor(image="img:1", bootstrap_url_template=TEMPLATE, exec_token="t")
    rs = RunSubmit(run_id="run_1", attempt=2, kind=RunKind.FIT, resource_class=ResourceClass.M)
    opts = ex.container_options(rs, "a" * 64)
    assert opts["network_mode"] == "none" and opts["read_only"] is True
    assert opts["cap_drop"] == ["ALL"] and "no-new-privileges:true" in opts["security_opt"]
    # the supervisor starts as root with only what the uid split needs; the worker runs as
    # 10001 with no capabilities at all (plan §8.1), and the runner refuses dev mode
    assert opts["user"] == "0:0" and opts["pids_limit"] == 512
    caps = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"]
    assert sorted(opts["cap_add"]) == caps
    assert opts["environment"]["MAPI_REQUIRE_ISOLATION"] == "1"
    assert opts["mem_limit"] == 16 * 1024**3 and opts["nano_cpus"] == 4_000_000_000
    assert opts["tmpfs"]["/tmp"].endswith(f"size={6 * 1024**3}")
    assert set(opts["environment"]) == {
        "MAPI_RUN_ID",
        "MAPI_NONCE",
        "MAPI_BOOTSTRAP_URL",
        "MAPI_EXEC_TOKEN",
        "MAPI_REQUIRE_ISOLATION",
        "PYTHONUNBUFFERED",
    }
    assert "runtime" not in opts and "command" not in opts
    assert ex.caps().isolation is Isolation.PROCESS and ex.caps().egress_enforced
    gv = DockerExecutor(image="img:1", bootstrap_url_template=TEMPLATE, gvisor=True, network="sb")
    gopts = gv.container_options(rs, "a" * 64)
    assert gopts["runtime"] == "runsc" and gopts["network_mode"] == "sb"
    assert gv.name == "docker-gvisor" and gv.caps().isolation is Isolation.GVISOR
    assert not gv.caps().egress_enforced
    # a non-root supervisor cannot split uids: never claim gVisor (public placement) then
    rootless = DockerExecutor(
        image="img:1", bootstrap_url_template=TEMPLATE, gvisor=True, user="10001:10001"
    )
    assert rootless.caps().isolation is Isolation.PROCESS
    assert not rootless.caps().allows_public_tenants()


def test_docker_image_is_chosen_per_plugin() -> None:
    ex = DockerExecutor(
        image="mapi/base-bayes:9",
        images={"ts": "mapi/base-forecast:9", "sklearn": "mapi/base-ml:9"},
        bootstrap_url_template=TEMPLATE,
    )

    def image(labels: dict[str, str]) -> str:
        rs = RunSubmit(run_id="run_1", kind=RunKind.FIT, labels=labels)
        return str(ex.container_options(rs, "a" * 64)["image"])

    assert image({PLUGIN_LABEL: "ts"}) == "mapi/base-forecast:9"
    assert image({PLUGIN_LABEL: "sklearn"}) == "mapi/base-ml:9"
    assert image({PLUGIN_LABEL: "mmm"}) == "mapi/base-bayes:9"  # not mapped: the default
    assert image({}) == "mapi/base-bayes:9"  # ingest / describe


def test_docker_settings_default_to_the_built_images() -> None:
    from mapi_server import __version__
    from mapi_server.executors import ExecutorRefused, build_executor
    from mapi_server.settings import ExecutorName

    s = Settings(database_url="postgresql://x/y")
    assert s.docker_image == f"mapi/base-bayes:{__version__}"  # what `make docker-build` tags
    assert s.docker_images == {
        "mmm": f"mapi/base-bayes:{__version__}",
        "tabular": f"mapi/base-bayes:{__version__}",
        "sklearn": f"mapi/base-ml:{__version__}",
        "ts": f"mapi/base-forecast:{__version__}",
    }
    # --network none cannot reach the HTTP bootstrap URL: refuse at start, not per run
    with pytest.raises(ExecutorRefused, match="MAPI_DOCKER_NETWORK"):
        build_executor(Settings(database_url="postgresql://x/y", executor=ExecutorName.DOCKER))
    ok = build_executor(
        Settings(
            database_url="postgresql://x/y",
            executor=ExecutorName.DOCKER,
            docker_network="container:mapi-gcs-shim",
        )
    )
    assert isinstance(ok, DockerExecutor)


async def test_docker_executor_lifecycle_with_a_fake_client() -> None:
    fake = _FakeDocker()
    ex = DockerExecutor(image="img:1", bootstrap_url_template=TEMPLATE, client_factory=lambda: fake)
    rs = RunSubmit(run_id="run_1", attempt=1, kind=RunKind.SCORE, timeout=Duration(60))
    h = await ex.submit(rs, "b" * 64)
    assert h.external_id == "c0ffee" and len(fake.containers.created) == 1
    assert await ex.submit(rs, "b" * 64) == h  # adopt by nonce label, no second container
    assert len(fake.containers.created) == 1
    import asyncio

    for _ in range(50):
        if not ex._followers[h.nonce].is_alive():
            break
        await asyncio.sleep(0.01)
    assert (await ex.status(h)).state is ExecState.RUNNING
    assert fake.containers.container is not None
    fake.containers.container.stop_delay = 1.0
    import time

    started = time.monotonic()
    await ex.cancel(h)
    await ex.cancel(h)  # a later reconcile pass: no second stop, no wait
    assert time.monotonic() - started < 0.5, "cancel must not block the reconcile pass"
    for _ in range(100):
        st = await ex.status(h)
        if st.state.terminal:
            break
        await asyncio.sleep(0.05)
    assert st.state is ExecState.CANCELLED and st.exit_code == 143
    assert fake.containers.container is not None
    fake.containers.container.attrs["State"] = {
        "Status": "exited",
        "ExitCode": 1,
        "OOMKilled": True,
    }
    ex._cancelled.clear()
    st = await ex.status(h)
    assert st.state is ExecState.FAILED and st.exit_code == 137


# ------------------------------------------------------------------ state machine service


async def test_apply_transition_agrees_with_the_contract_table(ctx: ServerContext) -> None:
    """Every (state, trigger, actor) combination behaves exactly like ``transition``."""
    async with ctx.db.session() as s:
        run = await create_run(
            s,
            Scope("default", "default"),
            kind=RunKind.FIT,
            resource_class=ResourceClass.S,
            timeout_s=60,
            payload={},
        )
        checked = 0
        for state, trigger, actor in itertools.product(RunState, RunTrigger, Actor):
            run.state, run.attempt, run.finished_at = state.value, 1, None
            run.failure_class = None
            try:
                expected = transition(state, trigger, actor=actor)
            except IllegalTransition:
                with pytest.raises(IllegalTransition):
                    await apply_transition(s, run, trigger, actor)
                continue
            rule = await apply_transition(s, run, trigger, actor)
            assert rule == expected and run.state == expected.target.value
            assert run.attempt == (2 if expected.new_attempt and not expected.noop else 1)
            if expected.failure_class is not None and not expected.noop:
                assert run.failure_class == expected.failure_class.value
                assert run.problem is not None
                assert run.problem["code"] == expected.failure_class.error_code.value
            if expected.target.terminal and not expected.noop:
                assert run.finished_at is not None
            checked += 1
        assert checked > 48


async def test_illegal_transition_maps_to_409(ctx: ServerContext) -> None:
    async with ctx.db.session() as s:
        run = await create_run(
            s,
            Scope("default", "default"),
            kind=RunKind.FIT,
            resource_class=ResourceClass.S,
            timeout_s=60,
            payload={},
        )
        with pytest.raises(IllegalTransition) as exc:
            await apply_transition(s, run, RunTrigger.LEASE, Actor.DISPATCHER)
        assert exc.value.status == 409
        with pytest.raises(IllegalTransition):  # wrong owner
            await apply_transition(s, run, RunTrigger.ACCEPT, Actor.DISPATCHER)
        with pytest.raises(NotImplementedError):
            await create_run(
                s,
                Scope("default", "default"),
                kind=RunKind.SWEEP,
                resource_class=ResourceClass.S,
                timeout_s=60,
                payload={},
            )
        rows = list((await s.execute(select(m.Run))).scalars())
        assert len(rows) == 1 and rows[0].created_at <= datetime.now(UTC)
