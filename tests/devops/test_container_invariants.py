"""Static invariants of the Dockerfiles, .dockerignore files, nginx templates, compose and .env.

Docker is not needed: everything is parsed from the files, and package-set claims are proven
against ``uv.lock`` (scripts/lock_closure.py). The images re-check the same claims at build
time against the real environment (images/common/assert_dists.py).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

import pytest
import yaml

from .conftest import ROOT, load_script

pytestmark = pytest.mark.unit

PY_IMAGES = ("base-bayes", "base-ml", "base-forecast", "server", "gcs-shim")
RUNNER_IMAGES = ("base-bayes", "base-ml", "base-forecast")
ALL_IMAGES = (*PY_IMAGES, "web")
SECRET_NAME = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL)", re.I)
OCI_LABELS = ("title", "description", "version", "revision", "created", "licenses", "vendor")


# ================================================================================ parsing


@dataclass(frozen=True)
class Instr:
    stage: int
    stage_name: str | None
    keyword: str
    args: str
    line: int


@cache
def dockerfile(name: str) -> tuple[Instr, ...]:
    text = (ROOT / "images" / name / "Dockerfile").read_text(encoding="utf-8")
    out: list[Instr] = []
    stage, stage_name = -1, None
    buf, start = "", 0
    for no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if buf and stripped.startswith("#"):
            continue  # comment lines inside a continuation are dropped by BuildKit
        if not buf:
            start = no
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        keyword, _, args = buf.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            stage += 1
            m = re.search(r"\bAS\s+(\S+)", args, re.I)
            stage_name = m.group(1) if m else None
        out.append(Instr(stage, stage_name, keyword, args.strip(), start))
        buf = ""
    return tuple(out)


def stage_names(name: str) -> list[str | None]:
    return [i.stage_name for i in dockerfile(name) if i.keyword == "FROM"]


def stage(name: str, stage_name: str | None = None) -> list[Instr]:
    """Instructions of a named stage (default: the last stage, i.e. the default target)."""
    instrs = dockerfile(name)
    if stage_name is None:
        last = max(i.stage for i in instrs)
        return [i for i in instrs if i.stage == last]
    return [i for i in instrs if i.stage_name == stage_name]


def arg_default(name: str, arg: str) -> str:
    for i in dockerfile(name):
        if i.keyword == "ARG" and i.args.split("=", 1)[0] == arg and "=" in i.args:
            return i.args.split("=", 1)[1].strip().strip('"')
    raise KeyError(f"{name}: no ARG {arg} with a default")


def env_vars(instrs: list[Instr]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in instrs:
        if i.keyword == "ENV":
            for key, value in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\S*)', i.args):
                out[key] = value.strip('"')
    return out


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that understands compose's merge tags (``!reset``, ``!override``)."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: None)
_ComposeLoader.add_constructor(
    "!override",
    lambda loader, node: (
        loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_scalar(node)
    ),
)


@cache
def compose(name: str = "docker-compose.yml") -> dict[str, Any]:
    data = yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert isinstance(data, dict)
    return data


@cache
def closure_mod() -> ModuleType:
    return load_script("scripts/lock_closure.py", "mapi_devtools_lock_closure")


@cache
def pins_mod() -> ModuleType:
    return load_script("scripts/image_pins.py", "mapi_devtools_image_pins")


def image_closure(*roots: str) -> set[str]:
    mod = closure_mod()
    lock = _lock()
    return set(mod.closure(lock, [mod.Requirement.parse(r) for r in roots]))


@cache
def _lock() -> Any:
    return closure_mod().Lock.load(ROOT / "uv.lock")


def dockerignore_excludes(patterns: list[str], path: str) -> bool:
    """Docker's .dockerignore semantics (last match wins; a match on a parent excludes)."""

    def rx(p: str) -> re.Pattern[str]:
        p = p.strip().lstrip("/")
        out, i = "", 0
        while i < len(p):
            if p.startswith("**/", i):
                out += "(?:.*/)?"
                i += 3
            elif p.startswith("**", i):
                out += ".*"
                i += 2
            elif p[i] == "*":
                out += "[^/]*"
                i += 1
            elif p[i] == "?":
                out += "[^/]"
                i += 1
            else:
                out += re.escape(p[i])
                i += 1
        return re.compile(out + r"$")

    rules = [(p.startswith("!"), rx(p.lstrip("!"))) for p in patterns]
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for negate, pattern in rules:
        if any(pattern.match(c) for c in candidates):
            excluded = not negate
    return excluded


def ignore_patterns(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


# ============================================================================ Dockerfiles


@pytest.mark.parametrize("name", ALL_IMAGES)
class TestDockerfile:
    def test_every_base_is_pinned_by_digest(self, name: str) -> None:
        pins = pins_mod()
        path = ROOT / "images" / name / "Dockerfile"
        found = pins.find_pins(path)
        assert found[0].name == "syntax" and found[0].line == 1
        assert any(p.name.endswith("_IMAGE") for p in found)
        assert pins.check(found) == []
        names = {p.name for p in found}
        earlier: set[str] = set()
        for instr in dockerfile(name):
            if instr.keyword == "FROM":
                ref = instr.args.split()[0]
                m = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", ref)
                assert (m and m.group(1) in names) or ref in earlier, (
                    f"{name}:{instr.line}: FROM {ref} must use a pinned *_IMAGE arg or a stage"
                )
                if instr.stage_name:
                    earlier.add(instr.stage_name)

    def test_final_stage_runs_as_non_root(self, name: str) -> None:
        users = [i.args for i in stage(name) if i.keyword == "USER"]
        assert users, f"{name}: the default target sets no USER"
        assert users[-1].split(":")[0] not in ("0", "root")
        assert users[-1] == ("101" if name == "web" else "10001:10001")

    def test_final_stage_has_exec_form_healthcheck(self, name: str) -> None:
        checks = [i.args for i in stage(name) if i.keyword == "HEALTHCHECK"]
        assert checks and "NONE" not in checks[-1].upper()
        cmd = checks[-1].split(" CMD ", 1)[1] if " CMD " in checks[-1] else ""
        assert json.loads(cmd), f"{name}: HEALTHCHECK must use the JSON exec form"

    def test_oci_labels(self, name: str) -> None:
        labels = " ".join(i.args for i in stage(name) if i.keyword == "LABEL")
        for key in OCI_LABELS:
            assert f"org.opencontainers.image.{key}=" in labels, f"{name}: missing {key}"

    def test_no_secrets_baked_in(self, name: str) -> None:
        text = (ROOT / "images" / name / "Dockerfile").read_text(encoding="utf-8")
        assert "BEGIN PRIVATE KEY" not in text and "BEGIN RSA" not in text
        for instr in dockerfile(name):
            if instr.keyword in ("ENV", "ARG"):
                for key in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", instr.args) or [
                    instr.args.split("=")[0]
                ]:
                    assert not SECRET_NAME.search(key), f"{name}:{instr.line}: {key}"
            if instr.keyword in ("COPY", "ADD"):
                assert ".env" not in instr.args.split(), f"{name}:{instr.line}"

    def test_no_add_and_no_latest(self, name: str) -> None:
        for instr in dockerfile(name):
            assert instr.keyword != "ADD", f"{name}:{instr.line}: use COPY"
            assert ":latest" not in instr.args

    def test_apt_hygiene(self, name: str) -> None:
        for instr in dockerfile(name):
            if instr.keyword == "RUN" and "apt-get install" in instr.args:
                assert "--no-install-recommends" in instr.args
                assert "rm -rf /var/lib/apt/lists/*" in instr.args
                assert "apt-get update" in instr.args

    def test_copy_sources_exist_and_survive_the_ignore_file(self, name: str) -> None:
        patterns = ignore_patterns(ROOT / "images" / name / "Dockerfile.dockerignore")
        for instr in dockerfile(name):
            if instr.keyword != "COPY" or "--from=" in instr.args:
                continue
            words = [w for w in instr.args.split() if not w.startswith("--")]
            for src in words[:-1]:
                if src == ".":
                    continue
                rel = src.rstrip("/")
                assert (ROOT / rel).exists(), f"{name}:{instr.line}: {src} does not exist"
                assert not dockerignore_excludes(patterns, rel), (
                    f"{name}:{instr.line}: {src} is excluded by its Dockerfile.dockerignore"
                )

    def test_dockerignore_keeps_secrets_and_state_out(self, name: str) -> None:
        root_rules = ignore_patterns(ROOT / ".dockerignore")
        own = ignore_patterns(ROOT / "images" / name / "Dockerfile.dockerignore")
        assert own[: len(root_rules)] == root_rules, "shared block drifted from .dockerignore"
        for path in (
            ".env",
            ".git",
            ".venv",
            "node_modules",
            ".local/pg",
            "plans/x.md",
            "docs/architecture-plan.md",
            "apps/web/node_modules/react",
            "packages/sdk/src/mapi_sdk/__pycache__/x.pyc",
        ):
            assert dockerignore_excludes(own, path), f"{name}: {path} would be sent"
        assert not dockerignore_excludes(own, ".env.example")


class TestDockerignoreMatcher:
    def test_semantics(self) -> None:
        pats = ["**/node_modules", "images/*", "!images/common", "*.md", "!README.md"]
        assert dockerignore_excludes(pats, "apps/web/node_modules/x")
        assert dockerignore_excludes(pats, "images/web/Dockerfile")
        assert not dockerignore_excludes(pats, "images/common/warm.py")
        assert dockerignore_excludes(pats, "NOTES.md")
        assert not dockerignore_excludes(pats, "README.md")
        assert not dockerignore_excludes(pats, "packages/x/README.md")  # *.md is root-only


# ====================================================================== runner images


class TestRunnerImages:
    def _shared(self, name: str) -> tuple[str, str]:
        text = (ROOT / "images" / name / "Dockerfile").read_text(encoding="utf-8")
        p1 = text.index("shared runner build: part 1")
        e1 = text.index("end of shared runner build: part 1")
        p2 = text.index("shared runner build: part 2")
        return text[p1:e1], text[p2:]

    def test_shared_sections_are_identical(self) -> None:
        first = self._shared("base-bayes")
        for name in RUNNER_IMAGES[1:]:
            assert self._shared(name) == first, f"{name} drifted from base-bayes"

    @pytest.mark.parametrize("name", RUNNER_IMAGES)
    def test_entrypoint_is_the_runner_under_tini(self, name: str) -> None:
        final = stage(name)
        entry = json.loads(next(i.args for i in final if i.keyword == "ENTRYPOINT"))
        cmd = json.loads(next(i.args for i in final if i.keyword == "CMD"))
        assert entry == ["/usr/bin/tini", "--", "/usr/local/bin/mapi-entrypoint"]
        assert cmd == ["mapi-runner"]
        assert stage_names(name)[-1] == "runtime"

    def test_caches_and_home_survive_worker_scrub_and_readonly_rootfs(self) -> None:
        from mapi_spec.run import WORKER_ENV_ALLOWLIST

        env = env_vars(stage("base-bayes", "runtime-base"))
        for key in ("PYTENSOR_FLAGS", "NUMBA_CACHE_DIR", "HOME"):
            assert key in WORKER_ENV_ALLOWLIST, f"{key} would be scrubbed from the worker"
        assert env["HOME"].startswith("/tmp/")
        assert env["NUMBA_CACHE_DIR"].startswith(env["MAPI_CACHE_DIR"])
        assert env["PYTENSOR_FLAGS"] == f"base_compiledir={env['MAPI_CACHE_DIR']}/pytensor"

    def test_worker_target(self) -> None:
        worker = stage("base-bayes", "worker")
        assert json.loads(next(i.args for i in worker if i.keyword == "CMD")) == [
            "mapi-server",
            "worker",
        ]
        assert any(i.keyword == "USER" and i.args == "10001:10001" for i in worker)
        assert any(i.keyword == "HEALTHCHECK" for i in worker)
        packages = (
            arg_default("base-bayes", "MAPI_PACKAGES").split()
            + arg_default("base-bayes", "MAPI_WORKER_PACKAGES").split()
        )
        names = image_closure(*packages)
        for dist in (
            "mapi-server",
            "mapi-runner",
            "mapi-plugin-mmm",
            "mapi-plugin-ts",
            "mapi-plugin-tabular",
            "mapi-sklearn",
            "docker",
        ):
            assert dist in names


@pytest.mark.parametrize("name", PY_IMAGES)
def test_build_time_assertions_agree_with_the_lock(name: str) -> None:
    """MAPI_ASSERT_PRESENT ⊆ closure and MAPI_ASSERT_ABSENT ∩ installed = ∅, per uv.lock."""
    packages = arg_default(name, "MAPI_PACKAGES").split()
    try:
        no_install = set(arg_default(name, "MAPI_NO_INSTALL").split())
    except KeyError:
        no_install = set()
    installed = image_closure(*packages) - no_install
    try:
        present = arg_default(name, "MAPI_ASSERT_PRESENT").split()
        absent = arg_default(name, "MAPI_ASSERT_ABSENT").split()
    except KeyError:  # gcs-shim asserts inline
        run = next(
            i.args for i in stage(name) if i.keyword == "RUN" and "assert_dists.py" in i.args
        )
        present = run.split("--present", 1)[1].split("--absent")[0].split()
        absent = run.split("--absent", 1)[1].split()
    assert set(present) <= installed, set(present) - installed
    assert not set(absent) & installed, set(absent) & installed


def test_control_plane_image_has_no_science_dependencies() -> None:
    names = image_closure(*arg_default("server", "MAPI_PACKAGES").split())
    forbidden = {
        "numpy",
        "xarray",
        "pandas",
        "scipy",
        "pyarrow",
        "zarr",
        "pymc",
        "pytensor",
        "mapi-sdk",
        "mapi-runner",
        "mapi-steps",
        "mapi-pymc",
        "mapi-sklearn",
        "mapi-plugin-mmm",
        "mapi-plugin-ts",
        "mapi-plugin-tabular",
    }
    assert not names & forbidden
    contracts = stage("server", "contracts")
    assert any("lint-imports" in i.args for i in contracts)
    assert any(i.keyword == "COPY" and "--from=contracts" in i.args for i in stage("server"))


#: The images that run ``mapi-server api|worker`` (compose, and the docker-executor override).
CONTROL_PLANE_STAGES = (("server", None), ("base-bayes", "worker"))


def _copies(instrs: list[Instr]) -> list[tuple[list[str], str]]:
    """``(sources, dest)`` of every ``COPY`` from the build context (not ``--from``)."""
    out = []
    for i in instrs:
        if i.keyword != "COPY" or "--from=" in i.args:
            continue
        parts = [p for p in i.args.split() if not p.startswith("--")]
        out.append((parts[:-1], parts[-1]))
    return out


@pytest.mark.parametrize(("name", "stage_name"), CONTROL_PLANE_STAGES)
def test_control_plane_images_resolve_the_image_digest(
    name: str, stage_name: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mapi-server api`` and ``worker`` key the catalog by the image digest at start-up
    (``Settings.resolve_image_digest``). The runtime images hold no ``uv.lock`` above their
    WORKDIR, so the lock is baked in and ``MAPI_UV_LOCK_PATH`` points at it; api and worker
    then agree on the digest (the same ``sha256(uv.lock)`` ``make dev`` uses)."""
    from mapi_server.settings import Settings, local_image_digest

    instrs = stage(name, stage_name)
    env = env_vars(instrs)
    assert "MAPI_IMAGE_DIGEST" not in env  # a literal digest would go stale with the lock
    lock_dest = env.get("MAPI_UV_LOCK_PATH")
    assert lock_dest, f"{name}:{stage_name or 'runtime'} does not set MAPI_UV_LOCK_PATH"
    assert any(srcs == ["uv.lock"] and dest == lock_dest for srcs, dest in _copies(instrs))
    # emulate the container: the lock at its baked path, a WORKDIR without one above it
    baked = tmp_path / "opt" / "uv.lock"
    baked.parent.mkdir()
    baked.write_bytes((ROOT / "uv.lock").read_bytes())
    workdir = tmp_path / "home" / "mapi"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)
    for key in [k for k in os.environ if k.startswith("MAPI_")]:
        monkeypatch.delenv(key)
    with pytest.raises(RuntimeError, match="image digest"):
        Settings().resolve_image_digest()  # what the images did before
    monkeypatch.setenv("MAPI_UV_LOCK_PATH", str(baked))
    assert Settings().resolve_image_digest() == local_image_digest(ROOT / "uv.lock")
    for svc in ("api", "worker", "migrate"):
        spec_env = compose()["services"][svc].get("environment") or {}
        assert "MAPI_IMAGE_DIGEST" not in spec_env and "MAPI_UV_LOCK_PATH" not in spec_env


def test_base_ml_has_no_pymc() -> None:
    names = image_closure(*arg_default("base-ml", "MAPI_PACKAGES").split())
    assert not names & {"pymc", "pytensor", "nutpie", "mapi-pymc", "pymc-marketing"}
    assert {"scikit-learn", "xgboost", "mapi-sklearn"} <= names


# ================================================================================ web image


@cache
def _templates() -> dict[str, str]:
    base = ROOT / "images" / "web" / "templates"
    return {
        str(p.relative_to(base)): p.read_text(encoding="utf-8")
        for p in base.rglob("*")
        if p.is_file()
    }


class TestWebImage:
    def test_template_variables_have_defaults(self) -> None:
        env = env_vars(stage("web"))
        allowed = set(env) | {"NGINX_LOCAL_RESOLVERS"}
        filt = re.compile(env["NGINX_ENVSUBST_FILTER"])
        for name, text in _templates().items():
            for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text):
                assert var in allowed, f"{name}: ${{{var}}} has no default ENV in the image"
                assert filt.search(var), f"{name}: ${{{var}}} is not substituted by the filter"

    def test_csp(self) -> None:
        t = _templates()
        app = t["web/web-headers.inc.template"]
        plots = t["plots/plots-headers.inc.template"]
        app_csp = re.search(r'Content-Security-Policy "([^"]+)"', app).group(1)  # type: ignore[union-attr]
        plots_csp = re.search(r'Content-Security-Policy "([^"]+)"', plots).group(1)  # type: ignore[union-attr]
        assert "frame-ancestors 'none'" in app_csp
        assert "frame-src ${MAPI_PLOTS_ORIGIN}" in app_csp
        assert "script-src 'self'" in app_csp and "object-src 'none'" in app_csp
        assert plots_csp.startswith("default-src 'none'")
        assert "frame-ancestors ${MAPI_WEB_ORIGIN}" in plots_csp
        assert "connect-src ${MAPI_PLOTS_ORIGIN}/topojson/" in plots_csp
        for text in (app, plots):
            assert "unsafe-eval" not in re.sub(r"#.*", "", text)
            assert "X-Content-Type-Options" in text and "Referrer-Policy" in text
        assert "/v1/" not in t["plots/plots.conf.template"]  # the sandbox cannot reach the API

    def test_headers_are_not_shadowed_by_location_add_header(self) -> None:
        for name, text in _templates().items():
            if name.endswith(".conf.template"):
                assert "add_header" not in text, f"{name}: add_header drops inherited headers"
                assert "include /tmp/nginx/conf.d/" in text

    def test_ports_and_braces(self) -> None:
        t = _templates()
        assert "listen 8080;" in t["web/web.conf.template"]
        assert "listen 8081;" in t["plots/plots.conf.template"]
        expose = next(i.args for i in stage("web") if i.keyword == "EXPOSE").split()
        assert expose == ["8080", "8081"]
        for text in [*t.values(), (ROOT / "images/web/nginx.conf").read_text()]:
            body = re.sub(r'"[^"]*"', "", re.sub(r"#.*", "", text))
            assert body.count("{") == body.count("}")

    def test_site_selection_directories(self) -> None:
        copies = [i.args for i in stage("web") if i.keyword == "COPY"]
        for site in ("web", "plots", "all"):
            assert any(c.endswith(f"/etc/nginx/templates/{site}/") for c in copies)


# ================================================================================== compose

MAPI_SERVICES = ("gcs-shim", "migrate", "api", "worker", "web", "plot-sandbox")


class TestCompose:
    def test_services(self) -> None:
        assert set(compose()["services"]) == {"postgres", *MAPI_SERVICES}

    @pytest.mark.parametrize("svc", ["postgres", *MAPI_SERVICES])
    def test_healthchecks(self, svc: str) -> None:
        spec = compose()["services"][svc]
        hc = spec.get("healthcheck", {})
        if svc == "migrate":
            assert hc.get("disable") is True and spec.get("restart") == "no"
        else:
            assert hc.get("test") and hc["test"][0] == "CMD"

    @pytest.mark.parametrize("svc", MAPI_SERVICES)
    def test_hardening(self, svc: str) -> None:
        spec = compose()["services"][svc]
        assert spec["user"].split(":")[0] not in ("0", "root")
        assert spec["read_only"] is True
        assert spec["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in spec["security_opt"]
        assert any(t.startswith("/tmp:") for t in spec["tmpfs"])
        assert spec["image"].startswith("mapi/")

    def test_third_party_images_pinned(self) -> None:
        pins = pins_mod()
        found = pins.find_pins(ROOT / "docker-compose.yml")
        assert [p.ref.split(":")[0] for p in found] == ["postgres"]
        assert pins.check(found) == []

    def test_no_literal_secrets(self) -> None:
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for svc, spec in compose()["services"].items():
            for key, value in (spec.get("environment") or {}).items():
                if SECRET_NAME.search(key):
                    assert re.fullmatch(r"\$\{[A-Z_]+:\?[^}]+\}", str(value)), f"{svc}.{key}"
        db = compose()["x-mapi-env"]["DATABASE_URL"]
        assert "${POSTGRES_PASSWORD:?" in db
        assert "docker.sock" not in text

    def test_ports_bind_localhost_and_match_localdev(self) -> None:
        from mapi_spec import localdev

        published: dict[int, int] = {}
        for spec in compose()["services"].values():
            for mapping in spec.get("ports", []):
                host_ip, host_port, container_port = str(mapping).split(":")
                assert host_ip == "127.0.0.1"
                published[int(host_port)] = int(container_port)
        assert published == {
            localdev.GCS_SHIM_PORT: 4443,
            localdev.API_PORT: 8000,
            localdev.WEB_PORT: 8080,
            localdev.PLOT_SANDBOX_PORT: 8081,
        }

    def test_shared_network_namespace_topology(self) -> None:
        from mapi_spec.localdev import LocalDev

        services = compose()["services"]
        for svc in ("api", "worker"):
            assert services[svc]["network_mode"] == "service:gcs-shim"
        assert "api" in services["gcs-shim"]["networks"]["default"]["aliases"]
        env, local = compose()["x-mapi-env"], LocalDev()
        assert env["MAPI_GCS_ENDPOINT"] == local.gcs_endpoint
        assert env["MAPI_API_URL"] == local.api_url
        assert env["MAPI_WEB_ORIGIN"] == local.web_origin
        assert env["MAPI_PLOTS_ORIGIN"] == local.plots_origin
        assert services["worker"]["environment"]["MAPI_EXECUTOR"].endswith("local-process}")

    def test_builds_reference_real_dockerfiles_and_targets(self) -> None:
        for svc, spec in compose()["services"].items():
            build = spec.get("build")
            if not build:
                continue
            path = build["dockerfile"]
            assert (ROOT / path).exists(), svc
            target = build.get("target")
            if target:
                assert target in stage_names(Path(path).parent.name)

    def test_override_example_is_valid_and_isolates_the_socket(self) -> None:
        data = compose("compose.override.example.yml")
        mounts = [
            (svc, v) for svc, spec in data["services"].items() for v in spec.get("volumes", [])
        ]
        assert mounts == [("worker", "/var/run/docker.sock:/var/run/docker.sock")]
        assert data["services"]["worker"]["environment"]["MAPI_EXECUTOR"] == "docker"
        assert data["services"]["postgres"]["ports"] == ["127.0.0.1:5432:5432"]


# ======================================================================= bundle-signing key

#: migrate creates the key; the worker's finalizer signs manifests; the API verifies them.
KEY_SERVICES = ("migrate", "api", "worker")


def _container_settings(svc: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``Settings()`` as ``svc`` builds it from its compose environment (literal values)."""
    from mapi_server.settings import Settings

    for key in [k for k in os.environ if k.startswith("MAPI_")]:
        monkeypatch.delenv(key)
    for key, value in (compose()["services"][svc].get("environment") or {}).items():
        if key.startswith("MAPI_") and "${" not in str(value):
            monkeypatch.setenv(key, str(value))
    return Settings(database_url="postgresql://mapi@postgres:5432/mapi")


def _mounts(svc: str) -> list[tuple[str, PurePosixPath, bool]]:
    out = []
    for spec in compose()["services"][svc].get("volumes", []):
        source, target, *mode = str(spec).split(":")
        out.append((source, PurePosixPath(target), "ro" in mode))
    return out


def _under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def test_bundle_signing_key_is_shared_and_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every process that signs or verifies bundle manifests loads ONE key, from a named
    volume that outlives restarts. A key under the per-container tmpfs (the old
    MAPI_DATA_DIR default) gave api and worker different keys, so every bundle the worker
    signed failed verification in the API, and each restart orphaned every model."""
    from mapi_server.app import load_signer
    from mapi_server.services.signing import BundleSigner

    named = set(compose().get("volumes") or {})
    sources, keys = set(), set()
    for svc in KEY_SERVICES:
        settings = _container_settings(svc, monkeypatch)
        key = PurePosixPath(str(settings.key_path))
        tmpfs = [PurePosixPath(t.split(":")[0]) for t in compose()["services"][svc]["tmpfs"]]
        assert not any(_under(key, t) for t in tmpfs), f"{svc}: {key} is on a tmpfs"
        mounts = [m for m in _mounts(svc) if _under(key, m[1])]
        assert mounts, f"{svc}: {key} is not on a volume"
        source, _, read_only = mounts[-1]
        assert source in named, f"{svc}: {source} is not a named volume"
        assert read_only is (svc != "migrate"), f"{svc}: only migrate may write the key"
        assert settings.signing_key_create is (svc == "migrate"), svc
        sources.add(source)
        keys.add(key)
    assert len(keys) == 1 and len(sources) == 1
    # the images own that directory, so a fresh named volume is writable by uid 10001
    key_dir = str(next(iter(keys)).parent)
    for name, stage_name in CONTROL_PLANE_STAGES:
        runs = " ".join(i.args for i in stage(name, stage_name) if i.keyword == "RUN")
        assert re.search(rf"install -d -o 10001 -g 10001 -m 0700 \S*{re.escape(key_dir)}\b", runs)

    # emulate the stack with the shared volume at tmp_path: migrate, then api and worker
    shared = tmp_path / "keys" / "bundle-signing-p256.pem"
    loaded = {}
    for svc in KEY_SERVICES:
        settings = _container_settings(svc, monkeypatch).model_copy(
            update={"signing_key_path": shared}
        )
        loaded[svc] = load_signer(settings)
    manifest = _signed_manifest(loaded["worker"])
    assert loaded["api"].verify(manifest)
    restarted = load_signer(
        _container_settings("api", monkeypatch).model_copy(update={"signing_key_path": shared})
    )
    assert restarted.verify(manifest)  # a restart keeps every bundle verifiable
    assert BundleSigner.from_file(shared, create=False).key_id == loaded["migrate"].key_id


def _signed_manifest(signer: Any) -> Any:
    from mapi_spec.run import BundleManifest

    example = BundleManifest.model_json_schema()["examples"][0]
    example.pop("signature")
    return signer.sign(BundleManifest.model_validate(example))


# ================================================================================ .env.example


def _mapi_names(text: str) -> set[str]:
    return set(re.findall(r"\bMAPI_[A-Z0-9_]*[A-Z0-9]\b", text))


def test_env_example_documents_every_runtime_variable(dev: ModuleType) -> None:
    from mapi_spec import localdev
    from mapi_spec import run as run_contract

    build_args: set[str] = set()
    used: set[str] = set()
    for name in ALL_IMAGES:
        build_args |= {i.args.split("=")[0] for i in dockerfile(name) if i.keyword == "ARG"}
        used |= _mapi_names((ROOT / "images" / name / "Dockerfile").read_text())
    for path in [
        ROOT / "docker-compose.yml",
        ROOT / "compose.override.example.yml",
        ROOT / "images/common/runner-entrypoint.sh",
        ROOT / "scripts/dev.py",
        *(ROOT / "images/web/templates").rglob("*.template"),
    ]:
        used |= _mapi_names(path.read_text(encoding="utf-8"))
    used |= {getattr(localdev, n) for n in dir(localdev) if n.startswith("ENV_")}
    used |= set(run_contract.RUNNER_ENV_VARS)
    used |= {dev.override_var(s) for s in ("gcs-shim", "migrate", "api", "worker", "web", "plots")}
    used -= build_args
    used = {u for u in used if u.startswith(("MAPI_", "DATABASE_URL"))}
    documented = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = sorted(v for v in used if not re.search(rf"\b{v}\b", documented))
    assert not missing, f".env.example does not document: {missing}"
    assert re.search(r"^POSTGRES_PASSWORD=change-me", documented, re.M)
