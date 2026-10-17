"""Rendering About cards in the terminal: plain language first, expert detail on request.

Every string comes from the server or a plugin and is printed through
:func:`~mapi_cli.output.safe` (no markup, no escape sequences).
"""

from __future__ import annotations

from collections.abc import Sequence

from mapi_spec.about import About, Recommendation
from rich.text import Text

from mapi_cli.output import Output, safe

__all__ = ["DIFFICULTY_STYLE", "print_about", "print_recommendations", "runtime_text"]

DIFFICULTY_STYLE = {"beginner": "green", "intermediate": "yellow", "expert": "magenta"}
_RUNTIME = {
    "instant": "under a second",
    "seconds": "seconds",
    "minutes": "minutes",
    "long": "more than 15 minutes",
}


def runtime_text(value: str) -> str:
    return _RUNTIME.get(value, value)


def _bullets(out: Output, title: str, items: Sequence[str]) -> None:
    if not items:
        return
    out.out.print(Text(title, style="bold"))
    for item in items:
        out.out.print(Text("  • ") + safe(item))


def print_about(
    out: Output, about: About, *, item_id: str | None = None, expert: bool = False
) -> None:
    """One About card: what it does, when to use it (and not), what it needs and gives."""
    head = safe(about.title, "bold")
    if item_id:
        head += Text("  ") + safe(item_id, "dim")
    out.out.print(head)
    out.out.print(safe(about.summary_plain))
    level = about.difficulty.value
    out.out.print(
        Text("difficulty: ", style="dim")
        + safe(level, DIFFICULTY_STYLE.get(level, ""))
        + Text("   typical time: ", style="dim")
        + safe(runtime_text(about.typical_runtime.value))
    )
    _bullets(out, "Use it when", about.use_when)
    _bullets(out, "Pick something else when", about.avoid_when)
    _bullets(out, "It needs", about.data_needs)
    _bullets(out, "It assumes", about.assumptions)
    _bullets(out, "You get", about.outputs)
    out.out.print(Text("Example: ", style="bold") + safe(about.example))
    if about.related:
        out.out.print(Text("Related: ", style="dim") + safe(", ".join(about.related)))
    if expert:
        if about.expert_notes:
            out.out.print(Text("Expert notes", style="bold"))
            out.out.print(safe(about.expert_notes))
        for ref in about.references:
            out.out.print(
                Text("  ref: ", style="dim") + safe(ref.title + (f" {ref.url}" if ref.url else ""))
            )
    elif about.expert_notes or about.references:
        out.out.print(Text("(--expert shows the maths and references)", style="dim italic"))


def print_recommendations(
    out: Output, items: Sequence[Recommendation], *, about: int = 3, expert: bool = False
) -> None:
    """The ranked list, then the About card of the first ``about`` items."""
    out.table(
        ["#", "item", "kind", "fit", "difficulty", "time", "why"],
        [
            (
                i,
                Text.assemble(safe(r.title, "bold"), "\n", safe(r.id, "dim")),
                r.kind.value,
                f"{r.score:.0%}",
                safe(r.difficulty.value, DIFFICULTY_STYLE.get(r.difficulty.value, "")),
                runtime_text(r.typical_runtime.value),
                Text("\n").join(
                    [safe(x) for x in r.reasons_plain]
                    + [safe(f"careful: {x}", "yellow") for x in r.cautions_plain]
                ),
            )
            for i, r in enumerate(items, 1)
        ],
        title="Recommended for you",
    )
    for r in items[:about]:
        if r.about is None:
            continue
        out.out.print()
        print_about(out, r.about, item_id=f"{r.kind.value} {r.id}", expert=expert)
