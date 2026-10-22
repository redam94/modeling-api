"""v0.18 recipe previews: the render job variant and the preview DTOs."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec import jobs
from mapi_spec.executor import RunKind
from mapi_spec.jobs import (
    PREVIEW_LABEL,
    PREVIEW_OUTPUT,
    PREVIEW_SETTINGS_MAX_BYTES,
    RecipePreview,
    RecipePreviewRequest,
    RecipePreviewResult,
    RenderJob,
    parse_job,
)
from pydantic import ValidationError


def test_a_render_job_previews_a_recipe() -> None:
    job = RenderJob(format="svg", recipe=RecipePreview(ref="core.ppc_fan@^1"))
    assert job.plots is None
    assert job.model_dump(mode="json")["recipe"] == {"ref": "core.ppc_fan@^1", "settings": {}}
    assert PREVIEW_OUTPUT == "preview/plot.json"
    assert PREVIEW_LABEL == "mapi.preview"


def test_recipe_and_plots_together_are_refused() -> None:
    with pytest.raises(ValidationError, match="leave plots unset"):
        RenderJob(plots=["ppc"], recipe={"ref": "core.ppc_fan@1.0.0"})  # type: ignore[arg-type]


def test_an_ordinary_render_job_is_unchanged() -> None:
    # recipe is left out when unset, so stored render RunSpecs keep their bytes
    assert RenderJob().model_dump(mode="json") == {
        "plots": None, "format": "png", "scale": 2.0, "width": None, "height": None,
    }  # fmt: skip
    assert RenderJob.model_validate({"plots": ["a"], "format": "pdf"}).recipe is None


@pytest.mark.parametrize("ref", ["", "not a ref", "core.ppc_fan@", "x" * 201])
def test_bad_refs_are_refused(ref: str) -> None:
    with pytest.raises(ValidationError):
        RecipePreview(ref=ref)


def test_settings_are_capped_at_4_kib() -> None:
    fits = {"title": "x" * (PREVIEW_SETTINGS_MAX_BYTES - 20)}
    RecipePreview(ref="core.ppc_fan@1.0.0", settings=fits)
    RecipePreviewRequest(settings=fits)
    too_big: dict[str, Any] = {"title": "x" * PREVIEW_SETTINGS_MAX_BYTES}
    with pytest.raises(ValidationError, match="4096"):
        RecipePreview(ref="core.ppc_fan@1.0.0", settings=too_big)
    with pytest.raises(ValidationError, match="4096"):
        RecipePreviewRequest(settings=too_big)
    with pytest.raises(ValidationError):
        RecipePreviewRequest.model_validate({"settings": {}, "extra": 1})


def test_preview_dtos_have_valid_examples() -> None:
    for model in (RecipePreviewRequest, RecipePreviewResult):
        examples = model.model_json_schema()["examples"]
        assert examples
        for ex in examples:
            assert model.model_validate(ex).model_dump(mode="json", exclude_none=True) == {
                k: v for k, v in ex.items() if v is not None
            }
    pending = RecipePreviewResult(run_id="run_01jaf3q2x8m9v7k5h4g3f2e1d0", done=False)
    assert pending.plot is None and not pending.cached


def test_parse_job_reads_a_preview_render_job() -> None:
    spec = {"format": "svg", "recipe": {"ref": "mmm.response_curves@^1", "settings": {}}}
    job = parse_job(RunKind.RENDER, spec)
    assert isinstance(job, jobs.RenderJob) and job.recipe is not None
    assert job.recipe.ref == "mmm.response_curves@^1"
