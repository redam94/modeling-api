"""``mapi catalog``: steps, models and plot recipes (local registry or the API)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer
from mapi_spec.catalog import Catalog

from mapi_cli.output import Output
from mapi_cli.state import cli_errors, state


class Section(StrEnum):
    ALL = "all"
    MODELS = "models"
    STEPS = "steps"
    PLOTS = "plots"


def _print(out: Output, cat: Catalog, section: Section, digest: str | None) -> None:
    if out.json_mode:
        doc = cat.model_dump(mode="json")
        if section is not Section.ALL:
            doc = {section.value: doc[section.value]}
        out.emit_json({"digest": digest, **doc})
        return
    if section in (Section.ALL, Section.MODELS):
        out.table(
            ["model", "version", "representation", "modes"],
            [
                (
                    m.id,
                    m.version,
                    m.manifest.capabilities.representation.value,
                    ", ".join(sorted(m.manifest.capabilities.score_modes)),
                )
                for m in cat.models
            ],
            title="Models",
        )
    if section in (Section.ALL, Section.STEPS):
        out.table(
            ["step", "version", "kind", "stateful", "invertible"],
            [
                (
                    s.id,
                    s.version,
                    s.kind.value,
                    "yes" if s.stateful else "",
                    "yes" if s.invertible else "",
                )
                for s in cat.steps
            ],
            title="Steps",
        )
    if section in (Section.ALL, Section.PLOTS):
        out.table(
            ["plot", "version", "title", "persona"],
            [(p.id, p.version, p.title, p.persona) for p in cat.plots],
            title="Plot recipes",
        )
    if digest:
        out.out.print(f"digest {digest}", markup=False, highlight=False)


@cli_errors
def catalog(
    ctx: typer.Context,
    section: Annotated[Section, typer.Argument(help="all, models, steps or plots")] = Section.ALL,
    remote: Annotated[
        str | None,
        typer.Option("--remote", help="Read the API's catalog ('@' = configured URL)"),
    ] = None,
) -> None:
    """Show the catalog of the locally installed plugins (or the API's with --remote)."""
    st = state(ctx)
    out = st.output
    if remote is not None:
        with st.client(remote) as api:
            res = api.catalog.get()
        cat = Catalog(models=res.models, steps=res.steps, plots=res.plots)
        _print(out, cat, section, res.digest)
        return
    from mapi_spec.run import catalog_digest

    from mapi_cli.local import load_registry

    registry, errors = load_registry()
    for err in errors:
        out.warn(f"plugin failed to load: {err}")
    cat = registry.catalog()
    _print(out, cat, section, catalog_digest(cat))
