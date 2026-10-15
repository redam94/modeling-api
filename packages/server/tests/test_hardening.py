"""Same-uid hardening of the long-running control-plane processes (local-process executor).

Under the local-process executor (``make dev`` and the default compose worker), every run's
supervisor and worker share the orchestrator's uid. The orchestrator therefore makes itself
non-dumpable: a same-uid process can then neither ptrace it nor read ``/proc/<pid>/environ``
(``DATABASE_URL`` with its password, ``MAPI_EXEC_TOKEN``) or its memory (the bundle-signing
key, the store's control key). What remains readable is stated in the start-up warning.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mapi_server import hardening
from mapi_server.settings import Settings

pytestmark = pytest.mark.unit


def test_harden_process_disables_core_dumps_and_ptrace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(hardening, "_prctl", lambda: lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(hardening.resource, "setrlimit", lambda r, v: limits.append((r, v)))
    assert hardening.harden_process() is True
    assert calls == [(hardening.PR_SET_DUMPABLE, 0, 0, 0, 0)]
    assert limits == [(hardening.resource.RLIMIT_CORE, (0, 0))]


def test_harden_process_without_prctl_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardening, "_prctl", lambda: None)
    monkeypatch.setattr(hardening.resource, "setrlimit", lambda r, v: None)
    assert hardening.harden_process() is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux-only")
def test_harden_process_really_marks_the_process_non_dumpable() -> None:
    code = (
        "import ctypes, ctypes.util\n"
        "from mapi_server.hardening import harden_process\n"
        "assert harden_process()\n"
        "libc = ctypes.CDLL(ctypes.util.find_library('c'))\n"
        "print(libc.prctl(3, 0, 0, 0, 0))\n"  # PR_GET_DUMPABLE
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0"


@pytest.mark.parametrize(
    ("argv", "hardened"),
    [(["api"], True), (["worker"], True), (["dev"], True), (["migrate"], False)],
)
def test_long_running_commands_harden_themselves(
    argv: list[str], hardened: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mapi_server.__main__ as cli

    seen: list[str] = []
    monkeypatch.setattr(cli, "harden_process", lambda: seen.append("harden") or True)
    for name in ("cmd_api", "cmd_worker", "cmd_dev", "cmd_migrate"):
        monkeypatch.setattr(cli, name, lambda *a, _n=name, **k: seen.append(_n) or 0)
    assert cli.main(argv) == 0
    assert ("harden" in seen) is hardened
    if hardened:
        assert seen[0] == "harden"  # before any secret is loaded


def test_local_process_executor_states_its_trust_boundary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from mapi_server.executors import build_executor

    settings = Settings(data_dir=tmp_path, exec_token="t", image_digest="sha256:" + "0" * 64)
    with caplog.at_level(logging.WARNING, logger="mapi_server.executors"):
        build_executor(settings)
    text = caplog.text
    assert "same uid" in text
    assert str(settings.key_path) in text
    assert str(settings.gcs_key_path("control")) in text
    assert "trusted" in text
