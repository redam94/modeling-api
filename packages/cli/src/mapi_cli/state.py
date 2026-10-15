"""Global CLI state (connection options, output mode) and error handling."""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, TypeGuard

import typer
from mapi_client import Client, ClientConfig, ConfigError, load_config
from mapi_spec.errors import MapiError

from mapi_cli.output import Output

__all__ = ["CONFIGURED", "EXIT_FAILURE", "AppState", "cli_errors", "state"]

EXIT_FAILURE: Final = 1
CONFIGURED: Final = "@"
"""``--remote @`` means "the configured API" (``--url``/``MAPI_URL``/profile/default)."""


@dataclass(slots=True)
class AppState:
    url: str | None = None
    token: str | None = None
    profile: str | None = None
    json_mode: bool = False
    output: Output = field(default_factory=Output)

    def config(self, url: str | None = None) -> ClientConfig:
        """Resolved client settings; ``url`` (from ``--remote``) overrides ``--url``."""
        explicit = None if url in (None, CONFIGURED) else url
        return load_config(url=explicit or self.url, token=self.token, profile=self.profile)

    def client(self, url: str | None = None) -> Client:
        return Client(config=self.config(url))


def _is_context(value: object) -> TypeGuard[typer.Context]:
    # typer vendors click, so its Context is not ``click.Context``: duck-type it
    return callable(getattr(value, "find_root", None)) and hasattr(value, "obj")


def state(ctx: typer.Context) -> AppState:
    obj = ctx.find_root().obj
    if not isinstance(obj, AppState):  # a sub-app invoked directly (tests)
        obj = AppState()
        ctx.find_root().obj = obj
    return obj


def cli_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Turn platform/config/IO errors into a readable message and exit status 1."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        ctx = next((a for a in [*args, *kwargs.values()] if _is_context(a)), None)
        out = state(ctx).output if ctx is not None else Output()
        try:
            return fn(*args, **kwargs)
        except MapiError as exc:
            out.error(exc)
        except ConfigError as exc:
            out.fail("CONFIG_ERROR", "configuration error", str(exc))
        except (FileNotFoundError, FileExistsError, IsADirectoryError, PermissionError) as exc:
            out.fail("FILE_ERROR", "error", str(exc))
        except TimeoutError as exc:
            out.fail("TIMEOUT", "timed out", str(exc))
        except ImportError as exc:
            out.fail("MISSING_DEPENDENCY", "missing dependency", str(exc))
        except ValueError as exc:  # argument/document validation (incl. pydantic errors)
            out.fail("INVALID_INPUT", "invalid input", str(exc))
        raise typer.Exit(EXIT_FAILURE)

    return wrapper
