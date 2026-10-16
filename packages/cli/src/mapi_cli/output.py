"""Terminal and JSON output.

Human output goes through rich; ``--json`` prints one JSON document to stdout for scripting
(diagnostics still go to stderr). Every string that came from the server, a plugin or a log
is *untrusted*: :func:`safe` strips ANSI/control sequences and wraps it in a rich ``Text``
so it is never interpreted as markup (plan §9: the CLI strips ANSI).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from mapi_client.errors import retry_after_of
from mapi_spec.cost import format_micros
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table
from rich.text import Text

__all__ = ["Output", "human_duration", "money", "safe", "strip_control", "to_jsonable"]

_ANSI: Final = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
# C0/C1 controls (not TAB/LF) and the bidi embeddings, overrides and isolates (text spoofing)
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def strip_control(text: str) -> str:
    """Remove ANSI escape sequences, control characters and bidi overrides (newlines and
    tabs are kept)."""
    return _CONTROL.sub("", _ANSI.sub("", text))


def safe(value: object, style: str = "") -> Text:
    """Untrusted value as plain rich ``Text`` (no markup, no escapes)."""
    return Text(strip_control("" if value is None else str(value)), style=style)


def human_duration(seconds: float) -> str:
    """``5400`` → ``1h 30m``; ``42`` → ``42s``."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "m")]
    text = " ".join(f"{n}{unit}" for n, unit in parts if n)
    return text if days or hours or not secs else f"{text} {secs}s"


def money(amount_micros: int | None, currency: str | None) -> str:
    """An exact amount (``31200, "USD"`` → ``0.031200 USD``); ``-`` when unknown."""
    if amount_micros is None:
        return "-"
    return format_micros(amount_micros, currency or "USD")


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    return value


_SEVERITY_STYLE: Final = {
    Severity.ERROR: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}


@dataclass(slots=True)
class Output:
    """Where and how a command reports results."""

    json_mode: bool = False
    out: Console = field(default_factory=lambda: Console(soft_wrap=True))
    err: Console = field(default_factory=lambda: Console(stderr=True, soft_wrap=True))

    # ------------------------------------------------------------------- generic
    def emit_json(self, value: Any) -> None:
        """Write a JSON document to stdout (bypasses rich so it is never re-wrapped)."""
        sys.stdout.write(json.dumps(to_jsonable(value), indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def info(self, message: str | Text) -> None:
        """Progress/diagnostic line on stderr (keeps stdout clean for --json).

        A ``str`` is printed verbatim (it often embeds server-supplied names and ids), never
        as rich markup; pass a :class:`~rich.text.Text` for styling.
        """
        if isinstance(message, Text):
            self.err.print(message)
        else:
            self.err.print(message, markup=False, highlight=False)

    def success(self, message: str) -> None:
        if not self.json_mode:
            self.out.print(Text(message, style="green"))

    def warn(self, message: str | Text) -> None:
        self.err.print(
            Text("warning: ", style="yellow")
            + (message if isinstance(message, Text) else Text(message))
        )

    def table(
        self, columns: Sequence[str], rows: Iterable[Sequence[object]], *, title: str | None = None
    ) -> None:
        table = Table(title=title, show_edge=False, header_style="bold")
        for col in columns:
            table.add_column(col, overflow="fold")
        for row in rows:
            table.add_row(*(v if isinstance(v, Text) else safe(v) for v in row))
        self.out.print(table)

    def kv(self, pairs: Iterable[tuple[str, object]], *, title: str | None = None) -> None:
        table = Table(title=title, show_header=False, show_edge=False, box=None)
        table.add_column(style="bold")
        table.add_column(overflow="fold")
        for key, value in pairs:
            if value is None or value == "" or value == [] or value == {}:
                continue
            table.add_row(key, value if isinstance(value, Text) else safe(value))
        self.out.print(table)

    # ------------------------------------------------------------------- issues
    def issues(self, issues: Sequence[Issue], *, title: str = "Issues") -> None:
        if not issues:
            return
        table = Table(title=title, show_edge=False, header_style="bold")
        for col in ("severity", "code", "pointer", "message"):
            table.add_column(col, overflow="fold")
        for i in issues:
            msg = safe(i.message)
            if i.suggested_action:
                msg.append("\n→ ").append(safe(i.suggested_action, "italic"))
            if i.untrusted_text:
                msg.append("\n").append(safe(i.untrusted_text, "dim"))
            table.add_row(
                Text(i.severity.value, style=_SEVERITY_STYLE.get(i.severity, "")),
                safe(i.code.value),
                safe(i.pointer or "/"),
                msg,
            )
        self.out.print(table)

    def fail(self, code: str, label: str, message: str) -> None:
        """A CLI-side failure (bad file, config, input): JSON on stdout with --json."""
        if self.json_mode:
            self.emit_json({"error": {"code": code, "detail": strip_control(message)}})
            return
        self.err.print(Text(label, style="bold red") + Text(": ") + safe(message))

    def error(self, exc: MapiError) -> None:
        """Report a platform error on stderr (and as JSON on stdout with --json)."""
        retry_after = retry_after_of(exc)
        if self.json_mode:
            doc: dict[str, Any] = {"error": exc.to_problem().model_dump(mode="json")}
            if retry_after is not None:
                doc["retry_after_seconds"] = retry_after
            self.emit_json(doc)
            return
        head = Text("error ", style="bold red") + safe(exc.code.value, "bold")
        head += Text(": ") + safe(exc.detail)
        self.err.print(head)
        if exc.issues and not (len(exc.issues) == 1 and exc.issues[0].message == exc.detail[:2000]):
            Output(json_mode=False, out=self.err, err=self.err).issues(exc.issues)
        if exc.untrusted_text:
            self.err.print(safe(exc.untrusted_text, "dim"))
        if retry_after is not None:
            at = datetime.now(UTC) + timedelta(seconds=retry_after)
            self.err.print(
                Text(
                    f"retry after {human_duration(retry_after)} "
                    f"(at {at.strftime('%Y-%m-%d %H:%M')} UTC)",
                    style="italic",
                )
            )
        hint = _HINTS.get(exc.code)
        if hint:
            self.err.print(Text(hint, style="italic"))


_HINTS: Final[dict[ErrorCode, str]] = {
    ErrorCode.RESOURCE_EXHAUSTED: "hint: retry at a larger resource class (mapi runs retry)",
    ErrorCode.UNAUTHENTICATED: (
        "hint: sign in with `mapi login` (or `mapi login --with-token`, or set MAPI_TOKEN)"
    ),
    ErrorCode.FORBIDDEN: (
        "hint: check your role and project with `mapi whoami`; tokens only act in their "
        "own project and within their scopes"
    ),
    ErrorCode.QUOTA_EXCEEDED: (
        "hint: see `mapi usage` for your quotas; wait for the window to reset, finish "
        "running work, or ask your tenant admin for more"
    ),
    ErrorCode.RATE_LIMITED: "hint: too many requests; slow down or retry later",
    ErrorCode.SCHEMA_DYNAMIC: "hint: the schema needs a dry-run to resolve (data-dependent)",
    # v0.5: workflows, governance, explorers, optimisation, the chooser
    ErrorCode.WORKFLOW_INVALID: (
        "hint: check the document with `mapi workflows validate FILE`; every problem has a "
        "JSON pointer"
    ),
    ErrorCode.WORKFLOW_NOT_RUNNABLE: (
        "hint: the revision has errors or the workflow is archived; fix it with `mapi "
        "workflows apply FILE` (or `mapi workflows archive WF --undo`)"
    ),
    ErrorCode.SCHEDULE_INVALID: (
        "hint: cron has five fields and may fire at most every 15 minutes; the time zone is "
        "an IANA name such as Europe/Berlin"
    ),
    ErrorCode.CHOOSER_ANSWER_INVALID: "hint: `mapi choose` lists the questions and answers",
    ErrorCode.SELF_APPROVAL_FORBIDDEN: (
        "hint: a second person with the reviewer capability must approve; ask a project admin"
    ),
    ErrorCode.VALIDATION_TRANSITION_INVALID: (
        "hint: see where it stands with `mapi validations get ID`; failed or rejected "
        "validations can be resubmitted"
    ),
    ErrorCode.NOT_VALIDATED: (
        "hint: a policy requires a validated version; run `mapi validate model MODEL_REF` "
        "and have it approved first"
    ),
    ErrorCode.POLICY_INVALID: "hint: `mapi governance checks` lists the checks and parameters",
    ErrorCode.OPTIMIZE_INFEASIBLE: (
        "hint: the bounds cannot meet the budget; widen --channel bounds or change --budget"
    ),
}
