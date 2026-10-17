"""``mapi choose`` and ``mapi about`` (v0.5): "Help me choose" in the terminal, and About
cards for anything a user can pick.

``mapi choose`` asks the chooser's questions one by one (numbers; several for questions
that take more than one answer; Enter skips) and shows the models, guided templates and
workflow templates that fit best, with the reasons and each one's About card. Scripts give
the answers up front: ``mapi choose --answer goal=forecast --answer data_shape=one_series``
asks nothing.
"""

from __future__ import annotations

from typing import Annotated

import typer
from mapi_spec.about import AboutKind, ChooserQuestion, ChooserQuestionnaire
from rich.text import Text

from mapi_cli.about import print_about, print_recommendations
from mapi_cli.output import Output, safe
from mapi_cli.state import cli_errors, state

__all__ = ["about", "choose"]


def _ask(out: Output, q: ChooserQuestion, n: int, total: int) -> list[str]:
    out.err.print(Text(f"\n{n}/{total} ", style="dim") + safe(q.prompt_plain, "bold"))
    if q.help_plain:
        out.err.print(safe(q.help_plain, "dim italic"))
    for i, c in enumerate(q.choices, 1):
        line = Text(f"  {i}) ") + safe(c.label)
        if c.help_plain:
            line += Text(" - ") + safe(c.help_plain, "dim")
        out.err.print(line)
    hint = "numbers separated by commas" if q.multiple else "a number"
    while True:
        raw = typer.prompt(
            f"  your answer ({hint}, Enter to skip)", default="", show_default=False, err=True
        )
        picks = [p.strip() for p in raw.split(",") if p.strip()]
        if not picks:
            return []
        try:
            idx = [int(p) for p in picks]
        except ValueError:
            idx = []
        if idx and all(1 <= i <= len(q.choices) for i in idx) and (q.multiple or len(idx) == 1):
            return list(dict.fromkeys(q.choices[i - 1].value for i in idx))
        out.err.print(Text(f"  please type {hint} between 1 and {len(q.choices)}", style="yellow"))


def _answers(items: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"--answer must look like question=answer[,answer], not {item!r}")
        out.setdefault(key.strip(), []).extend(v.strip() for v in value.split(",") if v.strip())
    return out


@cli_errors
def choose(
    ctx: typer.Context,
    answer: Annotated[
        list[str] | None,
        typer.Option(
            "--answer",
            "-a",
            help="question=answer (repeat; skips the questions); e.g. goal=forecast",
        ),
    ] = None,
    kind: Annotated[
        AboutKind | None,
        typer.Option("--kind", help="Rank one kind instead: model, step, node, check, ..."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=50)] = 5,
    show: Annotated[
        int, typer.Option("--about", min=0, help="About cards to show for the top items")
    ] = 3,
    expert: Annotated[bool, typer.Option("--expert", help="Show the maths and references")] = False,
) -> None:
    """Help me choose: a few questions about your goal and data, then the best fits."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        given = _answers(answer or [])
        if not given:
            questionnaire: ChooserQuestionnaire = api.catalog.chooser()
            out.err.print(
                Text("Help me choose", style="bold")
                + Text(" - answer what you can; Enter skips a question.", style="dim")
            )
            total = len(questionnaire.questions)
            for n, q in enumerate(questionnaire.questions, 1):
                picked = _ask(out, q, n, total)
                if picked:
                    given[q.id] = picked
        result = api.catalog.recommend(given, kind=kind, limit=limit)
    if out.json_mode:
        out.emit_json(result)
        return
    if not result.items:
        out.warn("nothing in the catalog fits those answers; try answering fewer questions")
        return
    out.out.print()
    print_recommendations(out, result.items, about=show, expert=expert)
    if result.unanswered:
        out.info("unanswered (answer them to sharpen the ranking): " + ", ".join(result.unanswered))


@cli_errors
def about(
    ctx: typer.Context,
    item_id: Annotated[str, typer.Argument(help="A model, step, node, check or template id")],
    kind: Annotated[AboutKind | None, typer.Option("--kind")] = None,
    expert: Annotated[bool, typer.Option("--expert", help="Show the maths and references")] = False,
) -> None:
    """The About card of anything a user can pick: what it does in plain words, when to use
    it and when not, what it needs, assumes and gives."""
    st = state(ctx)
    with st.client() as api:
        card = api.catalog.about(item_id, kind=kind)
    if st.output.json_mode:
        st.output.emit_json(card)
    else:
        print_about(st.output, card, item_id=item_id, expert=expert)
