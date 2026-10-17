"""Distribution version (kept in one place to avoid import cycles)."""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("mapi-client")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.5.0"
