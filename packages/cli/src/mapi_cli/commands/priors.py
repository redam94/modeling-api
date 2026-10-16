"""``mapi priors preview`` (v0.4): turn business beliefs into model priors.

Beliefs are ROI or contribution-share ranges per channel at a credibility (a YAML or JSON
``BusinessPriors`` file); lift tests are optional calibration data. A sandboxed
``translate_priors`` run converts them to model-space priors for a saved spec revision
(``--spec``/``--revision``) or an unsaved spec file (``--document``) and previews what the
model then believes, in business units, before seeing the KPI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_spec.api import PriorTranslation
from mapi_spec.calibration import DistributionSummary
from mapi_spec.loader import parse_text

from mapi_cli.output import Output
from mapi_cli.state import cli_errors, state

app = typer.Typer(
    help="Business priors: translate beliefs into model priors.", no_args_is_help=True
)


def _doc(path: Path) -> Any:
    """A YAML or JSON document (the safe, size-limited spec loader)."""
    return parse_text(path.read_text(encoding="utf-8"))


def _q(summary: DistributionSummary, level: str) -> str:
    value = summary.quantiles.get(level)
    return "" if value is None else f"{value:.3g}"


def _show(out: Output, got: PriorTranslation) -> None:
    result = got.result
    if result is None:
        out.info(f"run {got.run.id} is {got.run.state.value}; no result yet")
        return
    preview = result.preview
    rows = []
    for ch in result.channels:
        roi = preview.roi.get(ch)
        share = preview.contribution_share.get(ch)
        rows.append(
            (
                ch,
                f"{roi.mean:.3g} ({_q(roi, '0.05')} - {_q(roi, '0.95')})" if roi else "",
                f"{share.mean:.1%}" if share else "",
                f"{result.coverage[ch]:.0%}" if ch in result.coverage else "",
            )
        )
    out.table(
        ["channel", "prior ROI mean (5% - 95%)", "contribution share", "inside your range"],
        rows,
        title="What the model believes before seeing the KPI",
    )
    if preview.baseline_share is not None:
        out.kv([("baseline share", f"{preview.baseline_share.mean:.1%}")])
    out.issues(result.issues)
    out.info(f"apply the priors at {got.apply_at} (draws={result.draws}, seed={result.seed})")


@app.command("preview")
@cli_errors
def preview(
    ctx: typer.Context,
    beliefs: Annotated[
        Path,
        typer.Option("--beliefs", help="BusinessPriors (YAML/JSON)", exists=True, dir_okay=False),
    ],
    spec_id: Annotated[str | None, typer.Option("--spec", help="Saved spec id (spc_…)")] = None,
    revision: Annotated[int | None, typer.Option("--revision", min=1)] = None,
    document: Annotated[
        Path | None,
        typer.Option("--document", help="An unsaved spec (YAML)", exists=True, dir_okay=False),
    ] = None,
    lift_tests: Annotated[
        Path | None,
        typer.Option(
            "--lift-tests", help="A list of lift tests (YAML/JSON)", exists=True, dir_okay=False
        ),
    ] = None,
    dataset: Annotated[
        str | None, typer.Option("--dataset", help="Override the spec's data.dataset")
    ] = None,
    draws: Annotated[int, typer.Option("--draws", min=50, max=2000)] = 500,
    seed: Annotated[int | None, typer.Option("--seed", min=0)] = None,
    out_file: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the translation (JSON) here")
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Give up after N s")] = 600.0,
) -> None:
    """Translate beliefs into priors in the sandbox and preview them."""
    st = state(ctx)
    out = st.output
    if (spec_id is None) != (revision is None):
        raise ValueError("--spec and --revision go together")
    if (spec_id is None) == (document is None):
        raise ValueError("give --spec/--revision (a saved spec) or --document (a spec file)")
    priors = _doc(beliefs)
    tests = _doc(lift_tests) if lift_tests is not None else []
    if not isinstance(priors, dict):
        raise ValueError("--beliefs must hold a mapping (channels: …)")
    if not isinstance(tests, list):
        raise ValueError("--lift-tests must hold a list of lift tests")
    doc = _doc(document) if document is not None else None
    if doc is not None and not isinstance(doc, dict):
        raise ValueError("--document must be a ModelSpec mapping")
    with st.client() as api:
        out.info("translating the beliefs in the sandbox")
        got = api.guided.preview_priors(
            priors,
            spec_id=spec_id,
            revision=revision,
            document=doc,
            dataset=dataset,
            lift_tests=tests,
            draws=draws,
            seed=seed,
            timeout=timeout,
        )
    if out_file is not None and got.result is not None:
        out_file.write_text(
            json.dumps(got.result.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
    if out.json_mode:
        out.emit_json(got)
        return
    _show(out, got)
    if out_file is not None:
        out.success(f"wrote {out_file}")
