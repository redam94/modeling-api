"""Where the real pymc-challenges data files are, if this machine has them.

The ``from_challenges`` loaders read a local checkout of pymc-challenges (never the
network). Set ``MAPI_PYMC_CHALLENGES_DATA`` to its ``data/`` directory; tests that need the
real files skip when it is unset or the file is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["CHALLENGES_ENV", "challenges_file"]

CHALLENGES_ENV = "MAPI_PYMC_CHALLENGES_DATA"
"""Environment variable naming the pymc-challenges ``data/`` directory."""


def challenges_file(name: str) -> Path | None:
    """The path of ``name`` in the pymc-challenges data directory, or ``None`` if absent."""
    root = os.environ.get(CHALLENGES_ENV)
    if not root:
        return None
    path = Path(root).expanduser() / name
    return path if path.is_file() else None
