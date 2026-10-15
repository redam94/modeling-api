"""Shared helpers of the infrastructure tests (infra/terraform, deploy/gcp, deploy workflow).

The static tests read the HCL as text through a small block extractor (no HCL library is a
dependency of the repository); the Terraform tests run the real ``terraform`` binary in a
temporary copy of ``infra/terraform`` and are skipped when it is not installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra" / "terraform"
DEPLOY = ROOT / "deploy" / "gcp"
ENVS = ("dev", "staging", "prod")
MODULES = ("control", "exec", "data", "build", "perimeter", "stack")

# --------------------------------------------------------------------------- HCL as text


def strip_comments(text: str) -> str:
    """``text`` without ``#``, ``//`` and ``/* */`` comments (string and heredoc aware)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        heredoc = re.match(r"<<-?([A-Za-z_]\w*)\n", text[i:])
        if heredoc:
            end = re.compile(rf"^[ \t]*{heredoc.group(1)}[ \t]*$", re.M).search(
                text, i + heredoc.end()
            )
            stop = end.end() if end else n
            out.append(text[i:stop])
            i = stop
            continue
        ch = text[i]
        if ch == '"':
            j = _string_end(text, i)
            out.append(text[i:j])
            i = j
        elif ch == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _string_end(text: str, i: int) -> int:
    """Index just past the double-quoted string starting at ``text[i]`` (handles ``${...}``)."""
    j, depth = i + 1, 0
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if depth == 0 and c == '"':
            return j + 1
        if text.startswith("${", j) or text.startswith("%{", j):
            depth += 1
            j += 2
            continue
        if depth and c == "}":
            depth -= 1
        elif depth and c == '"':
            j = _string_end(text, j)
            continue
        j += 1
    return j


def _block_end(text: str, i: int) -> int:
    """Index just past the ``{ ... }`` block whose opening brace is ``text[i]``."""
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == '"':
            j = _string_end(text, j)
            continue
        heredoc = re.match(r"<<-?([A-Za-z_]\w*)\n", text[j:])
        if heredoc:
            end = re.compile(rf"^[ \t]*{heredoc.group(1)}[ \t]*$", re.M).search(
                text, j + heredoc.end()
            )
            j = end.end() if end else len(text)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise ValueError("unbalanced block")


@dataclass(frozen=True)
class Block:
    kind: str  # resource | data | module | variable | output | ephemeral | check
    type: str  # resource type, or the name for module/variable/output
    name: str  # resource name ("" for single-label blocks)
    body: str  # text between the braces, comments removed
    path: Path

    def attr(self, name: str) -> str | None:
        """Raw expression of a top-level-ish ``name = ...`` (first match, one line)."""
        m = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.+?)\s*$", self.body, re.M)
        return m.group(1) if m else None


_HEADER = re.compile(
    r'^(resource|data|module|variable|output|ephemeral|check)\s+"([^"]+)"(?:\s+"([^"]+)")?\s*\{',
    re.M,
)


def parse_blocks(path: Path) -> list[Block]:
    text = strip_comments(path.read_text())
    blocks: list[Block] = []
    for m in _HEADER.finditer(text):
        start = m.end() - 1
        end = _block_end(text, start)
        blocks.append(
            Block(m.group(1), m.group(2), m.group(3) or "", text[start + 1 : end - 1], path)
        )
    return blocks


def tf_files(*parts: str) -> list[Path]:
    return sorted((TF_ROOT.joinpath(*parts)).rglob("*.tf"))


@pytest.fixture(scope="session")
def all_blocks() -> list[Block]:
    return [b for p in tf_files() for b in parse_blocks(p)]


def resources(blocks: list[Block], rtype: str) -> list[Block]:
    return [b for b in blocks if b.kind == "resource" and b.type == rtype]


# ----------------------------------------------------------------------- terraform runner


@dataclass(frozen=True)
class Terraform:
    binary: str
    workdir: Path  # a copy of infra/terraform
    env: dict[str, str]

    def __call__(
        self, cwd: Path, *args: str, timeout: float = 900
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, f"-chdir={cwd}", *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
            check=False,
        )

    def init(self, cwd: Path) -> None:
        """``terraform init -backend=false``; skips the test when providers cannot be downloaded."""
        res = self(cwd, "init", "-backend=false", "-input=false", "-no-color")
        if res.returncode != 0:
            offline = re.search(
                r"(could not connect|Failed to query available provider packages|no such host|"
                r"connection refused|Failed to install provider|timeout)",
                res.stderr + res.stdout,
                re.I,
            )
            if offline:
                pytest.skip(
                    f"terraform providers unavailable (offline?): {res.stderr.strip()[:300]}"
                )
            pytest.fail(f"terraform init failed in {cwd}:\n{res.stdout}\n{res.stderr}")


@pytest.fixture(scope="session")
def terraform() -> Iterator[Terraform]:
    """A ``terraform`` runner over a temporary copy of infra/terraform.

    Providers come from ``TF_PLUGIN_CACHE_DIR`` when set (reused across runs); otherwise they
    are downloaded once per session into a temporary cache that is deleted afterwards (the
    Google providers are large).
    """
    binary = shutil.which("terraform")
    if binary is None:
        pytest.skip("terraform is not installed")
    with tempfile.TemporaryDirectory(prefix="mapi-tf-") as tmp:
        work = Path(tmp) / "terraform"
        shutil.copytree(
            TF_ROOT,
            work,
            ignore=shutil.ignore_patterns(
                ".terraform", "*.tfstate*", "terraform.tfvars", "backend.hcl"
            ),
        )
        env = dict(os.environ)
        if not env.get("TF_PLUGIN_CACHE_DIR"):
            cache = Path(tmp) / "plugin-cache"
            cache.mkdir()
            env["TF_PLUGIN_CACHE_DIR"] = str(cache)
        env.update({"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"})
        # Never let a test pick up real credentials or a real backend.
        for key in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CREDENTIALS",
            "GOOGLE_OAUTH_ACCESS_TOKEN",
        ):
            env.pop(key, None)
        yield Terraform(binary, work, env)
