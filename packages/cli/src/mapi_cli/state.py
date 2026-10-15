"""Global CLI state (connection options, output mode), confirmations and error handling."""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TypeGuard

import typer
from mapi_client import Client, ClientConfig, ConfigError, load_config
from mapi_client.config import ENV_PROFILE, config_path, read_config_file
from mapi_spec.errors import MapiError

from mapi_cli.output import Output

__all__ = [
    "CONFIGURED",
    "EXIT_FAILURE",
    "AppState",
    "cli_errors",
    "confirm",
    "state",
]

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
    project: str | None = None

    def config(self, url: str | None = None) -> ClientConfig:
        """Resolved client settings; ``url`` (from ``--remote``) overrides ``--url``."""
        explicit = None if url in (None, CONFIGURED) else url
        return load_config(
            url=explicit or self.url, token=self.token, profile=self.profile, project=self.project
        )

    def client(self, url: str | None = None) -> Client:
        return Client(config=self.config(url))

    @property
    def config_file(self) -> Path:
        return config_path()

    def target_profile(self) -> str | None:
        """The profile commands that write settings change: ``--profile``, ``MAPI_PROFILE``,
        else the file's default profile, else the top-level keys (``None``)."""
        if self.profile:
            return self.profile
        env = os.environ.get(ENV_PROFILE, "").strip()
        if env:
            return env
        default = read_config_file(self.config_file).get("profile")
        return default if isinstance(default, str) and default else None


def _is_context(value: object) -> TypeGuard[typer.Context]:
    # typer vendors click, so its Context is not ``click.Context``: duck-type it
    return callable(getattr(value, "find_root", None)) and hasattr(value, "obj")


def state(ctx: typer.Context) -> AppState:
    obj = ctx.find_root().obj
    if not isinstance(obj, AppState):  # a sub-app invoked directly (tests)
        obj = AppState()
        ctx.find_root().obj = obj
    return obj


def confirm(st: AppState, question: str, *, yes: bool) -> None:
    """Ask before a consequential change; ``--yes`` skips the question.

    The prompt goes to stderr (stdout stays clean for ``--json``); no answer (a script
    without ``--yes``, closed stdin) aborts with exit status 1.
    """
    if yes:
        return
    if not typer.confirm(question, default=False, err=True):
        st.output.fail("ABORTED", "aborted", "nothing was changed")
        raise typer.Exit(EXIT_FAILURE)


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
