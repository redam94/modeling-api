"""v0.15 contracts for deep learning: model side tables, the collection rules of
``validate_spec``, the GPU class hints, epoch checkpoints, the About cards of the ``dl``
models and plots, and the reference docs."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from mapi_spec import Catalog, DataSchema, ModelSpec
from mapi_spec.about import about_issues
from mapi_spec.abouts import DL_MODEL_IDS, MODEL_ABOUTS, PLANNED_MODEL_IDS, STEP_ABOUTS
from mapi_spec.api import V015_ENDPOINTS
from mapi_spec.capabilities import Capabilities, ModelManifest
from mapi_spec.errors import ErrorCode, Issue, Severity
from mapi_spec.plot_abouts import PLOT_ABOUTS
from mapi_spec.run import WORKER_ENV_ALLOWLIST
from mapi_spec.spec import Checkpoint, DataBlock, FitConfig, ScoreRequest
from mapi_spec.validate import ScoreMetadata, validate_score_request, validate_spec
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_IMAGE_DIMS: dict[str, Any] = {
    "image": {"kind": "categorical", "policy": "new_levels"},
    "y": {"kind": "integer", "policy": "fixed"},
    "x": {"kind": "integer", "policy": "fixed"},
    "channel": {"kind": "categorical", "policy": "fixed"},
}
_IMAGE_VARS: dict[str, Any] = {
    "pixels": {"dims": ["image", "y", "x", "channel"], "dtype": "uint8"},
    "label": {"dims": ["image"], "dtype": "str", "role": "target"},
}
RAW = {"dims": _IMAGE_DIMS, "vars": _IMAGE_VARS}

_CAPS: dict[str, Any] = {
    "inference": "custom",
    "representation": "draws",
    "score_modes": ["predict", "evaluate"],
    "checkpointable": True,
    "loo_valid": False,
    "loo_invalid_reason": "a network trained by gradient descent has no pointwise likelihood",
}

CNN: dict[str, Any] = {
    "id": "dl.finetune_classifier",
    "version": "1.0.0",
    "capabilities": {**_CAPS, "input": "collection"},
    "schema_template": {"template": {"dims": _IMAGE_DIMS, "vars": _IMAGE_VARS}},
    "settings_schema": {
        "type": "object",
        "properties": {"pretrained": {"type": "string", "default": "pretrained"}},
    },
    "fit_schema": {"type": "object"},
    "resources": {"min_class": "S", "default_class": "M", "gpu": True},
    "side_table_params": ["pretrained"],
}
LOGISTIC: dict[str, Any] = {
    **CNN,
    "id": "tabular.image_logistic",
    "capabilities": {**_CAPS, "inference": "mcmc", "checkpointable": False},
    "settings_schema": {"type": "object"},
    "resources": {"min_class": "S", "default_class": "S"},
    "side_table_params": [],
}
EMBED: dict[str, Any] = {
    "id": "dl.embed",
    "version": "1.0.0",
    "kind": "feature",
    "stateful": False,
    "settings_schema": {
        "type": "object",
        "properties": {"weights": {"type": "string", "default": "weights"}},
    },
    "effect": {},
    "side_table_params": ["weights"],
}


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.model_validate(
        {"steps": [EMBED], "models": [{"manifest": CNN}, {"manifest": LOGISTIC}], "plots": []}
    )


def _spec(model: str = "dl.finetune_classifier", **data: Any) -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {"name": "photos", "project": "p"},
        "data": {
            "dataset": "dataset://p/photos@v1",
            "side_tables": {"pretrained": {"ref": "dataset://p/tiny-resnet@v1"}},
            **data,
        },
        "model": {"use": f"{model}@^1", "fit": {"mode": "train_only"}},
    }


def _validate(catalog: Catalog, doc: dict[str, Any], **kw: Any) -> tuple[list[Issue], Any]:
    res = validate_spec(
        ModelSpec.model_validate(doc), catalog, DataSchema.model_validate(RAW), **kw
    )
    return res.issues, res.resolved


def _at(issues: list[Issue], pointer: str, severity: Severity | None = None) -> list[Issue]:
    return [
        i for i in issues if i.pointer == pointer and (severity is None or i.severity is severity)
    ]


# ============================================================================ side tables


def test_model_side_tables_resolve_like_step_side_tables(catalog: Catalog) -> None:
    issues, rs = _validate(catalog, _spec())
    assert not [i for i in issues if i.severity is Severity.ERROR], issues
    assert rs.model.side_tables == ["pretrained"]
    # the resolved spec stays plain JSON; the field is left out when empty
    assert type(rs).model_validate_json(rs.model_dump_json()) == rs
    doc = _spec()
    doc["model"]["settings"] = {"pretrained": "backbone"}
    issues, rs = _validate(catalog, doc)
    (err,) = _at(issues, "/model/settings/pretrained", Severity.ERROR)
    assert err.code is ErrorCode.REF_UNRESOLVED and "'backbone'" in err.message
    assert rs is None
    _, plain = _validate(catalog, _spec("tabular.image_logistic"))
    assert "side_tables" not in plain.model.model_dump(mode="json")


def test_manifest_side_table_params() -> None:
    man = ModelManifest.model_validate(CNN)
    assert man.side_table_params == ["pretrained"]
    assert "side_table_params" not in ModelManifest.model_validate(LOGISTIC).model_dump(mode="json")
    with pytest.raises(ValidationError, match="unique"):
        ModelManifest.model_validate({**CNN, "side_table_params": ["a", "a"]})
    with pytest.raises(ValidationError):
        ModelManifest.model_validate({**CNN, "side_table_params": [f"p{i}" for i in range(9)]})


def test_model_only_side_tables_are_never_needed_at_score(catalog: Catalog) -> None:
    doc = _spec()
    doc["data"]["side_tables"]["pretrained"]["score_policy"] = "supplied"
    _, rs = _validate(catalog, doc)
    meta = ScoreMetadata(resolved=rs)
    req = ScoreRequest.model_validate(
        {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "model://p/photos@v1"}
    )
    assert not [i for i in validate_score_request(req, meta) if i.pointer.startswith("/side")]
    # a step's supplied table stays needed
    doc = _spec("tabular.image_logistic")
    doc["data"]["side_tables"] = {
        "weights": {"ref": "dataset://p/tiny-resnet@v1", "score_policy": "supplied"}
    }
    doc["pipeline"] = [{"id": "embed", "use": "dl.embed@^1"}]
    _, rs = _validate(catalog, doc)
    issues = validate_score_request(req, ScoreMetadata(resolved=rs))
    assert [i.code for i in issues if i.pointer == "/side_tables/weights"] == [
        ErrorCode.SIDE_TABLE_COVERAGE
    ]


# ============================================================================ collections


def test_collection_with_a_batch_model_takes_no_steps(catalog: Catalog) -> None:
    doc = _spec()
    doc["data"]["side_tables"]["weights"] = {"ref": "dataset://p/tiny-resnet@v1"}
    doc["pipeline"] = [{"id": "embed", "use": "dl.embed@^1"}]
    issues, rs = _validate(catalog, doc, dataset_layout="collection")
    (err,) = _at(issues, "/pipeline", Severity.ERROR)
    assert err.message == "a collection dataset takes no pipeline steps"
    assert err.suggested_action is not None and "data.collection" in err.suggested_action
    assert rs is None
    # the same spec on a plain dataset is fine
    issues, _ = _validate(catalog, doc)
    assert not _at(issues, "/pipeline")


def test_collection_with_a_dataset_model_reads_a_sample(catalog: Catalog) -> None:
    doc = _spec("tabular.image_logistic", collection={"max_samples": 800})
    issues, rs = _validate(catalog, doc, dataset_layout="collection")
    (info,) = _at(issues, "/data/collection", Severity.INFO)
    assert "at most 800 images" in info.message and rs is not None
    issues, _ = _validate(catalog, _spec("tabular.image_logistic"), dataset_layout="collection")
    assert "at most 5000 images" in _at(issues, "/data/collection")[0].message
    # data.collection on a plain dataset: ignored with a warning
    issues, rs = _validate(catalog, doc)
    (warn,) = _at(issues, "/data/collection", Severity.WARNING)
    assert "ignores data.collection" in warn.message and rs is not None


@pytest.mark.parametrize(
    ("split", "ok"),
    [
        ({"kind": "stratified", "dim": "image", "by": "label", "fraction": 0.2}, True),
        ({"kind": "column", "dim": "image", "column": "split"}, True),
        ({"kind": "leave_group_out", "dim": "image", "n": 2}, True),
        ({"kind": "time_holdout", "dim": "image", "last": 2}, False),
    ],
)
def test_collection_takes_row_splits(catalog: Catalog, split: dict[str, Any], ok: bool) -> None:
    doc = _spec(split=split)
    issues, _ = _validate(catalog, doc, dataset_layout="collection")
    errs = _at(issues, "/data/split/kind", Severity.ERROR)
    assert (not errs) is ok
    if errs:
        assert errs[0].message == (
            "a collection takes row splits: column, stratified or leave_group_out"
        )


def test_collection_side_tables_are_refused(catalog: Catalog) -> None:
    issues, rs = _validate(catalog, _spec(), side_table_layouts={"pretrained": "collection"})
    (err,) = _at(issues, "/data/side_tables/pretrained", Severity.ERROR)
    assert "must be datasets" in err.message and rs is None
    issues, _ = _validate(catalog, _spec(), side_table_layouts={"pretrained": "dataset"})
    assert not _at(issues, "/data/side_tables/pretrained")


def test_data_block_collection_round_trip() -> None:
    block = DataBlock.model_validate(
        {"dataset": "dataset://p/photos@v1", "collection": {"max_samples": 10, "size": [8, 8]}}
    )
    assert block.collection is not None and block.collection.size == (8, 8)
    assert DataBlock.model_validate_json(block.model_dump_json()) == block
    plain = DataBlock.model_validate({"dataset": "dataset://p/photos@v1"})
    assert "collection" not in plain.model_dump(mode="json")


def test_capabilities_input_collection() -> None:
    caps = Capabilities.model_validate({**_CAPS, "input": "collection"})
    assert caps.input == "collection"
    assert "input" not in Capabilities.model_validate(_CAPS).model_dump(mode="json")


# ======================================================================== GPU, checkpoints


def test_gpu_class_hints(catalog: Catalog) -> None:
    doc = _spec("tabular.image_logistic")
    doc["execution"] = {"resource_class": "GPU"}
    # an error, not a warning: the GPU Job only runs models that use a GPU, so the run
    # would fail at every dispatch attempt
    issues, rs = _validate(catalog, doc)
    (err,) = _at(issues, "/execution/resource_class", Severity.ERROR)
    assert "does not use a GPU" in err.message and rs is None
    issues, _ = _validate(catalog, _spec())
    (info,) = _at(issues, "/execution/resource_class", Severity.INFO)
    assert info.message.startswith("the model can use the GPU class")
    doc = _spec()
    doc["execution"] = {"resource_class": "GPU"}
    issues, _ = _validate(catalog, doc)
    assert not _at(issues, "/execution/resource_class")


def test_checkpoint_every_epochs() -> None:
    assert Checkpoint(every_draws=100).model_dump(mode="json") == {"every_draws": 100}
    assert Checkpoint(every_epochs=2).model_dump(mode="json") == {"every_epochs": 2}
    for bad in ({}, {"every_draws": 1, "every_epochs": 1}):
        with pytest.raises(ValidationError, match="exactly one"):
            Checkpoint.model_validate(bad)
    with pytest.raises(ValidationError):
        Checkpoint(every_epochs=10_001)
    cfg = FitConfig.model_validate({"checkpoint": {"every_draws": 500}})  # old documents
    assert cfg.checkpoint is not None and cfg.checkpoint.every_draws == 500


def test_epoch_checkpoint_on_a_model_that_cannot_save_one(catalog: Catalog) -> None:
    doc = _spec("tabular.image_logistic")
    doc["model"]["fit"]["checkpoint"] = {"every_epochs": 2}
    issues, rs = _validate(catalog, doc)
    (warn,) = _at(issues, "/model/fit/checkpoint/every_epochs", Severity.WARNING)
    assert "ignores" in warn.message and rs is not None
    doc = copy.deepcopy(_spec())
    doc["model"]["fit"]["checkpoint"] = {"every_epochs": 2}
    issues, _ = _validate(catalog, doc)
    assert not _at(issues, "/model/fit/checkpoint/every_epochs")


def test_worker_env_allows_jax_device_settings() -> None:
    assert {
        "JAX_PLATFORMS",
        "XLA_PYTHON_CLIENT_PREALLOCATE",
        "XLA_PYTHON_CLIENT_MEM_FRACTION",
        "CUDA_VISIBLE_DEVICES",
        "XLA_FLAGS",
    } <= WORKER_ENV_ALLOWLIST


# ================================================================================== cards


def test_dl_cards() -> None:
    assert {"dl.mlp", "dl.cnn_classifier", "dl.finetune_classifier"} == DL_MODEL_IDS
    assert set(MODEL_ABOUTS) >= DL_MODEL_IDS and not DL_MODEL_IDS & PLANNED_MODEL_IDS
    for model in DL_MODEL_IDS:
        assert about_issues(MODEL_ABOUTS[model]) == [], model
        assert "classify" in MODEL_ABOUTS[model].fits["goal"]
    assert MODEL_ABOUTS["dl.cnn_classifier"].fits["data_shape"] == ["images"]
    assert "GPU" in (MODEL_ABOUTS["dl.cnn_classifier"].expert_notes or "")
    assert about_issues(STEP_ABOUTS["dl.embed"]) == []
    assert "features" in " ".join(MODEL_ABOUTS["tabular.image_logistic"].data_needs)
    for plot in ("ml.training_curves", "ml.confusion_epochs", "ml.misclassified"):
        assert about_issues(PLOT_ABOUTS[plot], known_ids=PLOT_ABOUTS) == [], plot


# =================================================================================== docs


def _docs() -> Path | None:
    return next(
        (p / "docs" for p in Path(__file__).resolve().parents if (p / "docs").is_dir()), None
    )


def test_reference_docs_cover_v015() -> None:
    docs = _docs()
    if docs is None or not (docs / "reference" / "http-api.md").exists():  # pragma: no cover
        pytest.skip("docs/ not available")
    text = (docs / "reference" / "http-api.md").read_text("utf-8")
    for e in V015_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            assert f"`{dto}`" in text, dto
    for dto in ("CollectionSource", "CollectionOptions", "CollectionInfo"):
        assert f"`{dto}`" in text, dto
    protocol = (docs / "reference" / "run-protocol.md").read_text("utf-8")
    for needle in (
        "`collection`",
        "input_layouts",
        "dataset.info",
        "dataset.pack.0000",
        "ckpt-<key>.weights.safetensors",
        "ckpt-<key>.opt.safetensors",
        "GPU_UNAVAILABLE",
    ):
        assert needle in protocol, needle
    spec_format = (docs / "reference" / "spec-format.md").read_text("utf-8")
    for needle in ("data.collection", "every_epochs", "safetensors"):
        assert needle in spec_format, needle
