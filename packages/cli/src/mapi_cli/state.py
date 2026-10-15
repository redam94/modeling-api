"""Global CLI state (connection options, output mode) and error handling."""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, ParamSpec, TypeVar

import typer
from mapi_client import Client, ClientConfig, ConfigError, load_config
from mapi_spec.errors import MapiError

from mapi_cli.output import Output

__all__ = ["CONFIGURED", "EXIT_FAILURE", "AppState", "cli_errors", "state"]

EXIT_FAILURE: Final = 1
CONFIGURED: Final = "@"
"""``--remote @`` means "the configured API" (``--url``/``MAPI_URL``/profile/default)."""

P = ParamSpec("P")
R = TypeVar("R")


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
        ctx = next((a for a in [*args, *kwargs.values()] if isinstance(a, typer.Context)), None)
        out = state(ctx).output if ctx is not None else Output()
        try:
            return fn(*args, **kwargs)
        except MapiError as exc:
            out.error(exc)
        except ConfigError as exc:
            out.err.print(f"[bold red]configuration error[/]: {exc}", markup=True)
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            out.err.print(f"[bold red]error[/]: {_escape(str(exc))}", markup=True)
        except TimeoutError as exc:
            out.err.print(f"[bold red]timed out[/]: {_escape(str(exc))}", markup=True)
        except ImportError as exc:
            out.err.print(f"[bold red]missing dependency[/]: {_escape(str(exc))}", markup=True)
        raise typer.Exit(EXIT_FAILURE)

    return wrapper


def _escape(text: str) -> str:
    from rich.markup import escape

    from mapi_cli.output import strip_control

    return escape(strip_control(text))


def kv_pairs(**values: Any) -> list[tuple[str, Any]]:
    return list(values.items())
