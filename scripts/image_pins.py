#!/usr/bin/env python3
"""Check and refresh the digest pins of every third-party container image.

Every base image in ``images/*/Dockerfile`` is declared once, as a build argument whose name
ends in ``_IMAGE``; the BuildKit frontend is the ``# syntax=`` directive on line 1; and every
third-party image in ``docker-compose.yml`` is an ``image:`` line. Each must carry a tag *and*
a ``@sha256:`` digest, so a rebuild months later uses the same bytes while the tag still tells
a reader what it is::

    ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:<64 hex>

Usage (stdlib only; ``update`` needs network access to the registries, nothing else does)::

    python scripts/image_pins.py check            # exit 1 if any image is not digest-pinned
    python scripts/image_pins.py list             # print every pin with its file and line
    python scripts/image_pins.py update           # re-resolve every tag, rewrite the digests

``update`` speaks the OCI distribution API directly (anonymous bearer-token flow), so it works
without a Docker daemon. Review the diff it produces like any dependency bump.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOCKER_HUB = "registry-1.docker.io"
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io"}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARG_RE = re.compile(r"^(?P<lead>\s*ARG\s+(?P<name>[A-Z0-9_]+_IMAGE)=)(?P<ref>\S+)\s*$")
_SYNTAX_RE = re.compile(r"^#\s*syntax=(?P<ref>\S+)\s*$")
_COMPOSE_RE = re.compile(r"^(?P<lead>\s*image:\s*)(?P<ref>[^\s#]+)\s*(?:#.*)?$")
_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

#: Images built from this repository are tagged by ``make docker-build`` and never pinned.
LOCAL_IMAGE_PREFIXES = ("mapi/",)


@dataclass(frozen=True, slots=True)
class ImageRef:
    """A parsed image reference ``[registry/]repository[:tag][@digest]``."""

    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @classmethod
    def parse(cls, ref: str) -> ImageRef:
        if not ref or any(c.isspace() for c in ref):
            raise ValueError(f"not an image reference: {ref!r}")
        name, _, digest = ref.partition("@")
        if digest and not _DIGEST_RE.match(digest):
            raise ValueError(f"malformed digest in {ref!r}")
        # A tag is the part after the last ':' that is not part of a registry host:port.
        tag: str | None = None
        last = name.rsplit("/", 1)[-1]
        if ":" in last:
            name, _, tag = name.rpartition(":")
        first, _, rest = name.partition("/")
        if rest and ("." in first or ":" in first or first == "localhost"):
            registry, repository = first, rest
        else:
            registry, repository = DOCKER_HUB, name
        if registry in _HUB_ALIASES:
            registry = DOCKER_HUB
            if "/" not in repository:
                repository = f"library/{repository}"
        if not repository:
            raise ValueError(f"not an image reference: {ref!r}")
        return cls(registry, repository, tag or None, digest or None)

    @property
    def pinned(self) -> bool:
        return self.digest is not None

    def display(self, digest: str | None = None) -> str:
        """Short human form (Docker Hub library images without a registry prefix)."""
        repo = self.repository
        if self.registry == DOCKER_HUB:
            repo = repo.removeprefix("library/")
        else:
            repo = f"{self.registry}/{repo}"
        out = repo + (f":{self.tag}" if self.tag else "")
        d = digest if digest is not None else self.digest
        return out + (f"@{d}" if d else "")


@dataclass(frozen=True, slots=True)
class Pin:
    path: Path
    line: int
    name: str
    ref: str

    @property
    def image(self) -> ImageRef:
        return ImageRef.parse(self.ref)


def _is_local(ref: str) -> bool:
    return ref.startswith(LOCAL_IMAGE_PREFIXES) or "${" in ref


def find_pins(path: Path, text: str | None = None) -> list[Pin]:
    """Every ``ARG *_IMAGE=`` (Dockerfiles) or third-party ``image:`` (compose) line."""
    body = path.read_text(encoding="utf-8") if text is None else text
    is_compose = path.suffix in {".yml", ".yaml"}
    out: list[Pin] = []
    for no, raw in enumerate(body.splitlines(), start=1):
        if is_compose:
            m = _COMPOSE_RE.match(raw)
            if m and not _is_local(m["ref"]):
                out.append(Pin(path, no, "image", m["ref"]))
        else:
            m = _ARG_RE.match(raw)
            if m:
                out.append(Pin(path, no, m["name"], m["ref"]))
            elif no == 1 and (s := _SYNTAX_RE.match(raw)):
                out.append(Pin(path, no, "syntax", s["ref"]))
    return out


def default_files(root: Path = ROOT) -> list[Path]:
    files = sorted((root / "images").glob("*/Dockerfile"))
    files += [p for p in (root / "docker-compose.yml",) if p.exists()]
    return files


def check(pins: Iterable[Pin]) -> list[str]:
    """Human-readable problems: unpinned, untagged or unparsable references."""
    problems: list[str] = []
    for pin in pins:
        where = f"{pin.path}:{pin.line}"
        try:
            image = pin.image
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if image.tag is None:
            problems.append(f"{where}: {pin.ref} has no tag (keep a readable tag before @sha256)")
        if not image.pinned:
            problems.append(f"{where}: {pin.ref} is not pinned by digest (run `make docker-pin`)")
    return problems


# ----------------------------------------------------------------------------- registry access

Opener = Callable[[urllib.request.Request], "urllib.response.addinfourl"]


def _parse_challenge(header: str) -> dict[str, str]:
    scheme, _, params = header.partition(" ")
    if scheme.lower() != "bearer":
        raise RuntimeError(f"unsupported registry auth scheme: {scheme!r}")
    return {k: v for k, v in re.findall(r'(\w+)="([^"]*)"', params)}


def _open(opener: Opener, req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
    try:
        with opener(req) as resp:
            status = int(resp.status or 0)
            return status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def resolve_digest(image: ImageRef, opener: Opener | None = None, timeout: float = 20.0) -> str:
    """The registry's current ``sha256:`` digest for ``image``'s tag (index for multi-arch)."""
    if image.tag is None:
        raise ValueError(f"{image.display()} has no tag to resolve")

    def _default(req: urllib.request.Request) -> urllib.response.addinfourl:
        return urllib.request.urlopen(req, timeout=timeout)

    op = opener or _default
    url = f"https://{image.registry}/v2/{image.repository}/manifests/{image.tag}"
    headers = {"Accept": _ACCEPT}
    status, resp_headers, _ = _open(op, urllib.request.Request(url, headers=headers, method="HEAD"))
    if status == 401:
        challenge = _parse_challenge(resp_headers.get("www-authenticate", ""))
        realm = challenge.pop("realm", "")
        if not realm.startswith("https://"):
            raise RuntimeError(f"refusing non-https token realm {realm!r}")
        challenge.setdefault("scope", f"repository:{image.repository}:pull")
        token_url = realm + "?" + urllib.parse.urlencode(challenge)
        t_status, _, body = _open(op, urllib.request.Request(token_url))
        if t_status != 200:
            raise RuntimeError(f"token request for {image.display()} failed: HTTP {t_status}")
        data = json.loads(body)
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RuntimeError(f"registry returned no token for {image.display()}")
        headers["Authorization"] = f"Bearer {token}"
        status, resp_headers, _ = _open(
            op, urllib.request.Request(url, headers=headers, method="HEAD")
        )
    if status != 200:
        raise RuntimeError(f"cannot resolve {image.display()}: HTTP {status}")
    digest = resp_headers.get("docker-content-digest", "")
    if not _DIGEST_RE.match(digest):
        raise RuntimeError(f"registry returned no usable digest for {image.display()}")
    return digest


def update_text(path: Path, text: str, resolver: Callable[[ImageRef], str]) -> str:
    """``text`` with every pin's digest replaced by ``resolver(image)``."""
    lines = text.splitlines(keepends=True)
    for pin in find_pins(path, text):
        image = pin.image
        new_ref = image.display(resolver(image))
        lines[pin.line - 1] = lines[pin.line - 1].replace(pin.ref, new_ref, 1)
    return "".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["check", "list", "update"], nargs="?", default="check")
    parser.add_argument(
        "files", nargs="*", type=Path, help="default: images/*/Dockerfile and docker-compose.yml"
    )
    args = parser.parse_args(argv)
    files = args.files or default_files()
    pins = [pin for f in files for pin in find_pins(f)]
    if args.command == "list":
        for pin in pins:
            print(
                f"{pin.path.relative_to(ROOT) if pin.path.is_absolute() else pin.path}:"
                f"{pin.line}\t{pin.name}\t{pin.ref}"
            )
        return 0
    if args.command == "update":
        cache: dict[str, str] = {}

        def resolver(image: ImageRef) -> str:
            key = image.display(digest="")
            if key not in cache:
                cache[key] = resolve_digest(image)
                print(f"resolved {key} -> {cache[key]}", file=sys.stderr)
            return cache[key]

        for f in files:
            text = f.read_text(encoding="utf-8")
            new = update_text(f, text, resolver)
            if new != text:
                f.write_text(new, encoding="utf-8")
                print(f"updated {f}", file=sys.stderr)
        return 0
    problems = check(pins)
    for p in problems:
        print(p, file=sys.stderr)
    if not pins:
        print("no image pins found", file=sys.stderr)
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
