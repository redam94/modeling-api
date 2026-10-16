"""``mapi plots render|embed|embeds|revoke`` (v0.4): static renders and embed links.

``render`` draws one plot - a saved plot, a model version's plot or a PlotSpec file - to
PNG, SVG or PDF in the sandbox and downloads it (CRC32C-verified). ``embed`` creates a
tokenised read-only link for a saved plot (platform recipes only): its URL carries the
token, so it is printed once, alone on stdout; listings show the redacted ``mpe_…`` form.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.text import Text

from mapi_cli.output import safe, strip_control
from mapi_cli.state import cli_errors, confirm, state

app = typer.Typer(help="Render plots to files and share them as embed links.", no_args_is_help=True)

Format = Literal["png", "svg", "pdf"]
_FORMATS: dict[str, Format] = {"png": "png", "svg": "svg", "pdf": "pdf"}
_STATUS_STYLE = {"active": "green", "expired": "yellow", "revoked": "red"}


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


@app.command("render")
@cli_errors
def render(
    ctx: typer.Context,
    saved: Annotated[str | None, typer.Option("--saved", help="A saved plot (plt_…)")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Model id (mdl_…)")] = None,
    version: Annotated[int | None, typer.Option("--version", min=1)] = None,
    plot: Annotated[str | None, typer.Option("--plot", help="Plot id of the model version")] = None,
    spec: Annotated[
        Path | None,
        typer.Option("--spec", help="A PlotSpec JSON file", exists=True, dir_okay=False),
    ] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="png, svg or pdf")] = "png",
    scale: Annotated[float, typer.Option("--scale", min=0.1, max=8.0)] = 2.0,
    width: Annotated[int | None, typer.Option("--width", min=16, max=4096)] = None,
    height: Annotated[int | None, typer.Option("--height", min=16, max=4096)] = None,
    out_file: Annotated[
        Path | None, typer.Option("--out", "-o", help="Where to write (default: <plot>.<fmt>)")
    ] = None,
    wait: Annotated[int, typer.Option("--wait", min=0, max=60, help="Prefer: wait=N")] = 60,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Give up after N seconds")
    ] = 300.0,
) -> None:
    """Render one plot to a PNG, SVG or PDF file."""
    st = state(ctx)
    out = st.output
    if fmt not in _FORMATS:
        raise ValueError("--format must be png, svg or pdf")
    sources = [saved is not None, model is not None or plot is not None, spec is not None]
    if sum(sources) != 1:
        raise ValueError("give exactly one of --saved, --model/--version/--plot or --spec")
    plot_spec = json.loads(spec.read_text(encoding="utf-8")) if spec is not None else None
    with st.client() as api:
        out.info("rendering in the sandbox")
        result = api.plots.render(
            plot_spec,
            saved_plot_id=saved,
            model_id=model,
            version=version,
            plot_id=plot,
            format=_FORMATS[fmt],
            scale=scale,
            width=width,
            height=height,
            wait=wait,
            timeout=timeout,
        )
        link = api.plots.render_output(result)
        dest = out_file or Path(Path(link.name).name)
        api.plots.download_render(result, dest)
    if out.json_mode:
        out.emit_json({"render": result, "out": str(dest)})
        return
    out.success(f"wrote {dest} ({link.size} bytes, run {result.run.id})")


@app.command("embed")
@cli_errors
def embed(
    ctx: typer.Context,
    plot_id: Annotated[str, typer.Argument(help="Saved plot id (plt_…)")],
    expires_in_days: Annotated[int, typer.Option("--expires-in-days", min=1, max=365)] = 30,
    label: Annotated[str | None, typer.Option("--label", help="To recognise the link by")] = None,
    allow_origin: Annotated[
        list[str] | None,
        typer.Option("--allow-origin", help="Site that may frame the link (repeatable)"),
    ] = None,
) -> None:
    """Create a read-only embed link for a saved plot. Its URL is printed once."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        created = api.plots.create_embed(
            plot_id,
            expires_in_days=expires_in_days,
            label=label,
            allowed_origins=allow_origin or [],
        )
    if out.json_mode:
        out.emit_json(created)  # explicitly requested: the token is included
        return
    out.info(
        Text(f"Created {created.embed.id} ({created.embed.display}), expires ")
        + Text(f"{_when(created.embed.expires_at)}. ")
        + Text("Copy the link now: it is not shown again.", style="bold yellow")
    )
    typer.echo(strip_control(created.url))


@app.command("embeds")
@cli_errors
def embeds(
    ctx: typer.Context,
    plot_id: Annotated[str, typer.Argument(help="Saved plot id (plt_…)")],
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
) -> None:
    """List a saved plot's embed links (redacted)."""
    st = state(ctx)
    with st.client() as api:
        items = api.plots.embeds(plot_id, limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "link", "label", "status", "views", "expires"],
        [
            (
                e.id,
                e.display,
                e.label or "",
                safe(e.status, _STATUS_STYLE.get(e.status, "")),
                e.views,
                _when(e.expires_at),
            )
            for e in items
        ],
    )


@app.command("revoke")
@cli_errors
def revoke(
    ctx: typer.Context,
    plot_id: Annotated[str, typer.Argument(help="Saved plot id (plt_…)")],
    embed_id: Annotated[str, typer.Argument(help="Embed id (emb_…)")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Revoke an embed link: dashboards showing it stop at once."""
    st = state(ctx)
    confirm(st, f"Revoke embed link {embed_id} of {plot_id}?", yes=yes)
    with st.client() as api:
        api.plots.revoke_embed(plot_id, embed_id)
    if st.output.json_mode:
        st.output.emit_json({"revoked": embed_id})
    else:
        st.output.success(f"Revoked {embed_id}")
