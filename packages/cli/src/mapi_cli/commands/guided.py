"""``mapi guided templates|detect|draft`` (v0.4): Guided mode from the command line.

``detect`` suggests a column for every question of a template, from a local CSV (profiled
here; only the column summaries are sent) or a stored dataset version. ``draft`` turns
answers into the ingest mapping and the ``ModelSpec`` (YAML) - nothing is stored, so the
result can be reviewed and saved with ``mapi datasets upload`` and the spec commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_spec.api import GuidedTemplate, RoleSuggestions
from mapi_spec.guided import ColumnProfile
from pydantic import JsonValue
from rich.text import Text

from mapi_cli.output import Output, safe, strip_control
from mapi_cli.profile import profile_csv
from mapi_cli.state import cli_errors, state

app = typer.Typer(
    help="Guided mode: templates, column detection and drafts (stores nothing).",
    no_args_is_help=True,
)

CsvOpt = Annotated[
    Path | None,
    typer.Option("--csv", help="Profile this CSV locally", exists=True, dir_okay=False),
]
DatasetOpt = Annotated[
    str | None, typer.Option("--dataset", help="A stored version: dataset://project/name@vN")
]


@app.command("templates")
@cli_errors
def templates(
    ctx: typer.Context,
    template_id: Annotated[str | None, typer.Argument(help="Show one template's questions")] = None,
) -> None:
    """List the guided templates, or one template's questions."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        if template_id is not None:
            tpl = api.guided.template(template_id)
            if out.json_mode:
                out.emit_json(tpl)
            else:
                _show_template(out, tpl)
            return
        items = list(api.guided.templates())
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "title", "family", "model", "tasks"],
        [
            (
                t.id,
                t.title,
                t.family,
                t.plugin if t.plugin_available else Text(f"{t.plugin} (not available)", "yellow"),
                ", ".join(t.tasks),
            )
            for t in items
        ],
    )


def _show_template(out: Output, tpl: GuidedTemplate) -> None:
    out.kv(
        [
            ("template", f"{tpl.id} {tpl.version}"),
            ("title", tpl.title),
            ("model", tpl.plugin),
            ("about", tpl.description_plain),
            ("note", tpl.plugin_note),
        ]
    )
    out.table(
        ["question", "kind", "asks", "required", "level"],
        [
            (q.id, q.kind, q.prompt_plain, "yes" if q.required else "no", q.ui_level)
            for q in tpl.questions
        ],
    )


def _columns(csv: Path | None, columns: Path | None) -> list[ColumnProfile] | None:
    if csv is not None and columns is not None:
        raise ValueError("give --csv or --columns, not both")
    if csv is not None:
        return profile_csv(csv)
    if columns is not None:
        doc = json.loads(columns.read_text(encoding="utf-8"))
        if not isinstance(doc, list):
            raise ValueError("--columns must be a JSON list of column profiles")
        return [ColumnProfile.model_validate(c) for c in doc]
    return None


def _show_suggestions(out: Output, got: RoleSuggestions) -> None:
    out.table(
        ["question", "answer", "best candidate", "why"],
        [
            (
                q.question_id,
                json.dumps(q.answer, ensure_ascii=False) if q.answer is not None else "-",
                f"{q.candidates[0].column} ({q.candidates[0].confidence:.0%})"
                if q.candidates
                else "-",
                q.candidates[0].reason_plain if q.candidates else "",
            )
            for q in got.questions
        ],
    )
    if got.unassigned:
        out.info(Text("columns no answer uses: ") + safe(", ".join(got.unassigned)))
    out.issues(got.issues)


@app.command("detect")
@cli_errors
def detect(
    ctx: typer.Context,
    template_id: Annotated[str, typer.Argument(help="Template id (geo_mmm, …)")],
    csv: CsvOpt = None,
    dataset: DatasetOpt = None,
    columns: Annotated[
        Path | None,
        typer.Option("--columns", help="Column profiles (JSON list)", exists=True, dir_okay=False),
    ] = None,
) -> None:
    """Suggest which column answers each question of a template."""
    st = state(ctx)
    out = st.output
    cols = _columns(csv, columns)
    if (cols is None) == (dataset is None):
        raise ValueError("give a --csv (or --columns) file or a --dataset reference")
    with st.client() as api:
        got = api.guided.detect_roles(template_id, dataset=dataset, columns=cols)
    if out.json_mode:
        out.emit_json(got)
    else:
        _show_suggestions(out, got)


def parse_answer(text: str) -> tuple[str, JsonValue]:
    """``key=value``: JSON when it parses (numbers, lists), a comma list, else a string."""
    key, sep, raw = text.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ValueError(f"--answer {text!r}: expected QUESTION=VALUE")
    raw = raw.strip()
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        value = [p.strip() for p in raw.split(",") if p.strip()] if "," in raw else raw
    return key, value


@app.command("draft")
@cli_errors
def draft(
    ctx: typer.Context,
    template_id: Annotated[str, typer.Argument(help="Template id (geo_mmm, …)")],
    name: Annotated[str, typer.Option("--name", help="Name of the spec (and model)")],
    dataset: Annotated[str, typer.Option("--dataset", help="Name the upload's dataset gets")],
    answer: Annotated[
        list[str] | None,
        typer.Option("--answer", "-a", help="QUESTION=VALUE (repeatable; lists: a,b,c)"),
    ] = None,
    answers_file: Annotated[
        Path | None,
        typer.Option("--answers", help="Answers as a JSON object", exists=True, dir_okay=False),
    ] = None,
    csv: CsvOpt = None,
    detect_first: Annotated[
        bool, typer.Option("--detect", help="Pre-fill answers from detection (needs --csv)")
    ] = False,
    fmt: Annotated[str, typer.Option("--format", help="csv, parquet or mff")] = "csv",
    yaml_out: Annotated[
        Path | None, typer.Option("--yaml-out", help="Write the spec YAML here")
    ] = None,
    mapping_out: Annotated[
        Path | None, typer.Option("--mapping-out", help="Write the ingest mapping JSON here")
    ] = None,
) -> None:
    """Answers -> the ingest mapping and the ModelSpec YAML (nothing is stored)."""
    st = state(ctx)
    out = st.output
    answers: dict[str, JsonValue] = {}
    if answers_file is not None:
        doc = json.loads(answers_file.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("--answers must be a JSON object")
        answers.update(doc)
    explicit = dict(parse_answer(a) for a in answer or [])
    cols = profile_csv(csv) if csv is not None else None
    if detect_first and cols is None:
        raise ValueError("--detect needs --csv")
    with st.client() as api:
        if detect_first:
            suggested = api.guided.detect_roles(template_id, columns=cols)
            for q in suggested.questions:
                if q.answer is not None:
                    answers.setdefault(q.question_id, q.answer)
        answers.update(explicit)
        fields: dict[str, Any] = {
            "template_id": template_id,
            "name": name,
            "dataset": dataset,
            "format": fmt,
            "answers": answers,
        }
        if cols is not None:
            fields["columns"] = [c.model_dump(mode="json") for c in cols]
        got = api.guided.draft(**fields)
    mapping = got.mapping.model_dump(mode="json", by_alias=True, exclude_none=True)
    if yaml_out is not None:
        yaml_out.write_text(got.yaml, encoding="utf-8")
    if mapping_out is not None:
        mapping_out.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    if out.json_mode:
        out.emit_json(got)
        return
    out.kv(
        [
            ("template", f"{got.template_id} {got.template_version}"),
            ("dataset", got.dataset_ref),
            ("answers", json.dumps(answers, ensure_ascii=False)),
        ]
    )
    out.issues(got.issues, title="Worth a second look")
    if yaml_out is None:
        typer.echo(strip_control(got.yaml))
    else:
        out.success(f"wrote {yaml_out}")
    if mapping_out is not None:
        out.success(f"wrote {mapping_out}")
