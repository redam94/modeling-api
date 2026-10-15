"""Reading a fixture's ``spec.yaml`` and ``score_requests.yaml``.

``score_requests.yaml`` maps a request name to ``{payload, side_tables, request}``:
``payload`` names the score data the fixture's data module builds (``future``,
``window``, ...), ``side_tables`` says whether the fixture's side tables are passed, and
``request`` is a ScoreRequest document.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from mapi_spec import ModelSpec, ScoreRequest, load_model_spec

__all__ = ["NamedRequest", "read_requests", "read_spec"]


@dataclass(frozen=True, slots=True)
class NamedRequest:
    """One entry of ``score_requests.yaml``."""

    name: str
    payload: str
    side_tables: bool
    request: ScoreRequest


def read_spec(fixture_dir: Path) -> ModelSpec:
    return load_model_spec((fixture_dir / "spec.yaml").read_text())


def read_requests(fixture_dir: Path) -> list[NamedRequest]:
    raw: Any = yaml.safe_load((fixture_dir / "score_requests.yaml").read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{fixture_dir.name}/score_requests.yaml must be a mapping")
    out = []
    for name, entry in raw.items():
        if not isinstance(entry, dict) or set(entry) - {"payload", "side_tables", "request"}:
            raise ValueError(f"{fixture_dir.name}: request {name!r} is malformed")
        out.append(
            NamedRequest(
                name=str(name),
                payload=str(entry["payload"]),
                side_tables=bool(entry.get("side_tables", True)),
                request=ScoreRequest.model_validate(entry["request"]),
            )
        )
    return out
