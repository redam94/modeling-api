"""The `mapi` command line."""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("mapi-cli")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.10.0"
