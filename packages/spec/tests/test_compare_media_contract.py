"""v0.14 contracts: media/experiment/view/comparison DTOs and their HTTP table, image-folder
ingest, the column and stratified splits, RGB and gallery views, the classification cards,
the reference docs, and the run-query test vectors shared with the web UI.

The vectors: ``MAPI_UPDATE_SNAPSHOTS=1 uv run pytest`` on this file writes
``tests/data/run_query_vectors.json`` and the web copy
``apps/web/src/features/runs/run-query.vectors.gen.json`` from the Python parsers; otherwise
the test checks that both files are current.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from mapi_spec import api
from mapi_spec.about import CHOOSER_QUESTIONS
from mapi_spec.abouts import (
    CLASSIFICATION_MODEL_IDS,
    MODEL_ABOUTS,
    PLANNED_MODEL_IDS,
    SPLIT_ABOUTS,
)
from mapi_spec.api import (
    ROUTE_ACCESS,
    V014_ENDPOINTS,
    CompareMetric,
    CompareSeries,
    ExperimentCreate,
    ExperimentPatch,
    ExperimentRunsWrite,
    MediaWrite,
    RunCompare,
    RunCreate,
    RunPatch,
    RunViewCreate,
    RunViewPatch,
    RunViewQuery,
    TrackingFrame,
)
from mapi_spec.audit import AuditAction
from mapi_spec.endpoints import schema_breaks
from mapi_spec.ingest import (
    IMAGE_FOLDER_MAX_CELLS,
    RESERVED_IMAGE_DIMS,
    ImageFolderOptions,
    IngestAssembly,
    IngestMapping,
)
from mapi_spec.media import MAX_API_MEDIA_BYTES
from mapi_spec.plot_abouts import PLOT_ABOUTS
from mapi_spec.schema import NUMERIC_DTYPES, DataSchema
from mapi_spec.spec import ColumnSplit, DataBlock, StratifiedSplit, split_value_key
from mapi_spec.tenancy import Action, ResourceKind
from mapi_spec.tracking import (
    MAX_COMPARE_RUNS,
    MAX_EXPERIMENT_RUNS_WRITE,
    MAX_RUN_FILTERS,
    parse_run_filter,
    parse_run_order,
    parse_run_ref,
)
from mapi_spec.views import (
    MAX_GALLERY_ITEMS,
    FieldEntry,
    GalleryItem,
    GalleryView,
    ImageEntry,
    RgbRaster,
)
from pydantic import TypeAdapter, ValidationError

pytestmark = pytest.mark.unit

RUN = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
RUN2 = "run_01jaf3q2x8m9v7k5h4g3f2e1d1"


def _png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    raw = b"".join(b"\x00" + bytes(width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ========================================================================================= media


def test_media_write_decodes_and_checks_the_bytes() -> None:
    body = MediaWrite(key="samples", kind="image", content_type="image/png", data=_b64(_png()))
    assert body.decoded() == _png()
    table = json.dumps({"columns": ["a"], "rows": [[1]]}).encode()
    MediaWrite(key="t", kind="table", content_type="application/json", data=_b64(table))
    for over, match in (
        ({"data": "not base64!"}, "base64"),
        ({"data": _b64(_png())[:-2]}, "base64"),
        ({"data": _b64(b"GIF89a....")}, "not a PNG"),
        ({"kind": "table"}, "does not match"),
        ({"content_type": "image/jpeg"}, "not a JPEG"),
        ({"key": "a/b"}, "pattern"),
        ({"caption": "x" * 501}, "at most 500"),
        ({"step": -1}, "greater than or equal"),
        ({"labels": {"Bad": "x"}}, "pattern"),
    ):
        doc = {"key": "samples", "kind": "image", "content_type": "image/png"}
        doc |= {"data": _b64(_png())} | over
        with pytest.raises(ValidationError, match=match):
            MediaWrite.model_validate(doc)


def test_media_write_size_caps() -> None:
    field = MediaWrite.model_fields["data"]
    cap = next(c.max_length for c in field.metadata if hasattr(c, "max_length"))
    assert cap == 4 * ((MAX_API_MEDIA_BYTES + 2) // 3)
    # a valid PNG padded past the API cap: the decoded size is refused
    big = _png() + bytes(MAX_API_MEDIA_BYTES)
    with pytest.raises(ValidationError, match="at most"):
        MediaWrite(key="k", kind="image", content_type="image/png", data=_b64(big))


def test_tracking_frame_and_run_dtos_carry_v014_fields() -> None:
    assert TrackingFrame(run_id=RUN).media is False
    assert TrackingFrame(run_id=RUN, media=True).media
    patch = RunPatch(experiment_id=None)
    assert "experiment_id" in patch.model_fields_set
    assert RunPatch(experiment_id="exp_1").experiment_id == "exp_1"
    create = RunCreate.model_validate(
        {"kind": "fit", "spec_id": "spc_1", "revision": 1, "experiment_id": "exp_1"}
    )
    assert create.experiment_id == "exp_1"
    run_example = api.Run.model_json_schema()["examples"][0]
    assert api.Run.model_validate(run_example | {"experiment_id": "exp_1"}).experiment_id


# =================================================================== experiments, saved views


def test_experiment_names_are_stripped_and_checked() -> None:
    assert ExperimentCreate(name="  adstock study ").name == "adstock study"
    for bad in ("", "   ", "a\nb", "tab\there", "x" * 201):
        with pytest.raises(ValidationError):
            ExperimentCreate(name=bad)
    with pytest.raises(ValidationError, match="at most 20000"):
        ExperimentCreate(name="ok", description="x" * 20_001)
    with pytest.raises(ValidationError, match="at least one"):
        ExperimentPatch()
    assert ExperimentPatch(archived=True).archived
    assert "description" in ExperimentPatch(description=None).model_fields_set


def test_experiment_runs_write_rules() -> None:
    assert ExperimentRunsWrite(add=[RUN]).add == [RUN]
    with pytest.raises(ValidationError, match="at least one run id"):
        ExperimentRunsWrite()
    with pytest.raises(ValidationError, match="in add and in remove"):
        ExperimentRunsWrite(add=[RUN, RUN2], remove=[RUN2])
    with pytest.raises(ValidationError):
        ExperimentRunsWrite(add=[f"run_{i}" for i in range(MAX_EXPERIMENT_RUNS_WRITE + 1)])


def test_run_view_query_parses_with_the_tracking_grammar() -> None:
    q = RunViewQuery(
        kind="fit",
        tags=["team:growth", "mapi.source", "candidate"],
        filters=["metric.rmse < 3", "param.lr = 0.01", "tag.team ~ grow"],
        order_by="-metric.loss{split=val}.min",
        columns=["metric.rmse", "param.settings.l_max", "created_at"],
    )
    assert q.order_by == "-metric.loss{split=val}.min"
    for over, match in (
        ({"filters": ["metric.rmse"]}, "no operator"),
        ({"filters": ["metric.rmse ~ 3"]}, "takes one of"),
        ({"filters": ["metric.rmse < abc"]}, "not a number"),
        ({"order_by": "bogus"}, "order_by"),
        ({"columns": ["metric.Bad"]}, "run ref"),
        ({"tags": ["bad key:x"]}, "tag key"),
        ({"filters": ["name = a"] * (MAX_RUN_FILTERS + 1)}, "at most"),
        ({"state": "nope"}, "Input should be"),
        ({"unknown": 1}, "Extra inputs"),
    ):
        with pytest.raises(ValidationError, match=match):
            RunViewQuery.model_validate(over)
    view = RunViewCreate(name=" best ", query=q)
    assert view.name == "best"
    with pytest.raises(ValidationError, match="name or the query"):
        RunViewPatch()
    assert RunViewPatch(query=RunViewQuery()).query == RunViewQuery()


# ==================================================================================== compare


def test_compare_shapes_and_caps() -> None:
    with pytest.raises(ValidationError, match="same length"):
        CompareSeries(run_id=RUN, attempt=1, count=2, steps=[1, 2], values=[0.1])
    example = RunCompare.model_json_schema()["examples"][0]
    cmp = RunCompare.model_validate(example)
    assert [r.id for r in cmp.runs] == [RUN, RUN2]
    assert cmp.params[0].differs and cmp.truncated == {"params": False}
    with pytest.raises(ValidationError):
        RunCompare.model_validate(example | {"runs": example["runs"][:1]})
    too_many = [example["runs"][0] | {"id": f"run_{i}"} for i in range(MAX_COMPARE_RUNS + 1)]
    with pytest.raises(ValidationError):
        RunCompare.model_validate(example | {"runs": too_many})
    with pytest.raises(ValidationError):
        RunCompare.model_validate(example | {"truncated": {"bogus": True}})
    assert CompareMetric(key="loss").series == []


# ================================================================================ HTTP table


def test_v014_endpoints_access_and_audit() -> None:
    start = api.ENDPOINTS.index(V014_ENDPOINTS[0])
    assert api.ENDPOINTS[start : start + len(V014_ENDPOINTS)] == V014_ENDPOINTS
    by_op = {e.operation_id: e for e in V014_ENDPOINTS}
    assert len(by_op) == len(V014_ENDPOINTS) == 16
    for e in V014_ENDPOINTS:
        rule = ROUTE_ACCESS[e.operation_id]
        assert rule.resource is ResourceKind.RUN
        assert rule.action is (Action.READ if e.method == "GET" else Action.UPDATE), e
        assert api.PROJECT_HEADER in e.headers, e
        if e.paginated:
            assert {"limit", "cursor"} <= set(e.query), e
    assert by_op["log_run_media"].success == 201
    assert api.ErrorCode.PAYLOAD_TOO_LARGE in by_op["log_run_media"].errors
    assert by_op["compare_runs"].path == "/v1/run-compare"
    assert set(by_op["compare_runs"].query) == {"run", "key", "media_key", "max_points", "params"}
    assert by_op["delete_experiment"].success == by_op["delete_run_view"].success == 204
    list_runs = next(e for e in api.ENDPOINTS if e.operation_id == "list_runs")
    assert "experiment_id" in list_runs.query
    actions = {a.value for a in AuditAction}
    assert {"experiment.create", "experiment.update", "experiment.delete", "run_view.save",
            "run_view.delete"} <= actions  # fmt: skip


# ============================================================================ image ingest


def test_image_folder_mapping() -> None:
    m = IngestMapping.model_validate({"format": "image_folder"})
    assert m.images is None
    assert "images" not in m.model_dump(mode="json")
    m = IngestMapping.model_validate(
        {"format": "image_folder", "images": {"label_column": "class", "size": [32, 32]}}
    )
    assert m.images is not None and m.images.size == (32, 32) and m.images.dim == "image"
    for doc, match in (
        ({"format": "csv", "images": {}}, "only used with format 'image_folder'"),
        (
            {"format": "image_folder", "vars": {"a": {"column": "a"}}},
            "described by 'images'",
        ),
        ({"format": "image_folder", "preset": "mff"}, "described by 'images'"),
    ):
        with pytest.raises(ValidationError, match=match):
            IngestMapping.model_validate(doc)


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"dim": "sample"}, "reserved"),
        ({"dim": "chain"}, "reserved"),
        ({"dim": "channel"}, "reserved"),
        ({"manifest": "../m.csv"}, "relative path"),
        ({"manifest": "/m.csv"}, "relative path"),
        ({"manifest": "a\\m.csv"}, "relative path"),
        ({"manifest": "m.xlsx"}, "must end with"),
        ({"size": [0, 10]}, "1..4096"),
        ({"size": [10, 5000]}, "1..4096"),
        ({"resize": "crop"}, "only used with size"),
        ({"label_column": "path"}, "two roles"),
        ({"var": "image"}, "different names"),
        ({"max_images": 0}, "greater than or equal"),
        ({"max_images": 100_001}, "less than or equal"),
        ({"mode": "cmyk"}, "Input should be"),
    ],
)
def test_image_folder_options_refusals(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ImageFolderOptions.model_validate(over)


def test_image_folder_constants_and_assembly() -> None:
    assert "sample" in RESERVED_IMAGE_DIMS and IMAGE_FOLDER_MAX_CELLS == 256 * 1024**2
    assert ImageFolderOptions(manifest="data/labels.parquet").manifest == "data/labels.parquet"
    src = {"upload_id": "upl_1", "mapping": {"format": "image_folder"}}
    assert IngestAssembly.model_validate({"sources": [src]}).sources[0].mapping.images is None
    other = {"upload_id": "upl_2", "mapping": {"format": "netcdf"}, "combine": "merge"}
    with pytest.raises(ValidationError, match="only source"):
        IngestAssembly.model_validate({"sources": [src | {"combine": "merge"}, other]})


def test_uint8_is_a_numeric_dtype() -> None:
    assert "uint8" in NUMERIC_DTYPES
    schema = DataSchema.model_validate(
        {
            "dims": {
                "image": {"kind": "categorical", "policy": "new_levels"},
                "y": {"kind": "integer"},
                "x": {"kind": "integer"},
                "channel": {"kind": "categorical"},
            },
            "vars": {"pixels": {"dims": ["image", "y", "x", "channel"], "dtype": "uint8"}},
        }
    )
    assert schema.vars["pixels"].dtype == "uint8"
    as_float = schema.model_copy(
        update={"vars": {"pixels": schema.vars["pixels"].model_copy(update={"dtype": "float64"})}}
    )
    assert schema_breaks(schema, as_float) == []  # same numeric family


# ===================================================================================== splits


def test_column_and_stratified_splits() -> None:
    split = TypeAdapter(DataBlock).validate_python(
        {"dataset": "dataset://default/shapes@v1", "split": {"kind": "column", "dim": "image",
                                                             "column": "split"}}
    ).split  # fmt: skip
    assert isinstance(split, ColumnSplit) and split.holdout == ["test"]
    assert split.split_dims() == ["image"] and split.forecast_dim is None
    with pytest.raises(ValidationError, match="in holdout and in train"):
        ColumnSplit(kind="column", dim="image", column="split", holdout=["1"], train=[1])
    # the check compares values as the split does: 1.0, 1 and " 1" are the same value
    for holdout, train in (([1.0], [1, 2]), (["1"], [1.0]), ([" a"], ["a"])):
        with pytest.raises(ValidationError, match="in holdout and in train"):
            ColumnSplit(kind="column", dim="fold", column="fold", holdout=holdout, train=train)
    assert [split_value_key(v) for v in (1.0, 2, "3", " x ", float("nan"), True)] == [
        "1",
        "2",
        "3",
        "x",
        "",
        "True",
    ]
    ColumnSplit(kind="column", dim="fold", column="fold", holdout=[1.5], train=[1, 2])
    with pytest.raises(ValidationError):
        ColumnSplit(kind="column", dim="image", column="split", holdout=[])
    s = StratifiedSplit(kind="stratified", dim="image", by="label", fraction=0.25, seed=3)
    assert s.split_dims() == ["image"]
    for frac in (0.0, 1.0):
        with pytest.raises(ValidationError):
            StratifiedSplit(kind="stratified", dim="image", by="label", fraction=frac)
    for kind in ("core.column", "core.stratified"):
        about = SPLIT_ABOUTS[kind]
        assert about.title and any("time" in a for a in about.avoid_when)
    assert SPLIT_ABOUTS["core.column"].title == "Split by a column"
    assert SPLIT_ABOUTS["core.stratified"].title == "Stratified random holdout"


# ====================================================================================== views


def _rgb(cells: int) -> str:
    return _b64(bytes(3 * cells))


def _entry(**over: Any) -> dict[str, Any]:
    raster = {"lo": 0.0, "hi": 1.0, "data": _b64(bytes(2 * 4))}
    doc: dict[str, Any] = {
        "output": "pixels",
        "y_dim": "y",
        "x_dim": "x",
        "height": 2,
        "width": 2,
        "source_height": 2,
        "source_width": 2,
        "y_coords": [0, 1],
        "x_coords": [0, 1],
        "frames": [{"label": "0", "mean": raster, "rgb": {"data": _rgb(4)}}],
        "color": "rgb",
        "channel_dim": "channel",
    }
    return doc | over


def test_rgb_rasters_need_whole_triplets_and_the_cell_count() -> None:
    assert RgbRaster(data=_rgb(5)).cells() == 5
    with pytest.raises(ValueError, match="triplets"):
        RgbRaster(data=_b64(bytes(4))).cells()
    with pytest.raises(ValueError, match="not base64"):
        RgbRaster(data="!!").cells()
    entry = FieldEntry.model_validate(_entry())
    assert entry.cells() == 4 * 3  # mean + an rgb raster counted twice
    frame = _entry()["frames"][0]
    for over, match in (
        ({"frames": [frame | {"rgb": {"data": _rgb(3)}}]}, "needs 4 cells"),
        ({"frames": [{k: v for k, v in frame.items() if k != "rgb"}]}, "rgb raster in every"),
        ({"color": "single"}, "only an rgb field"),
    ):
        with pytest.raises(ValidationError, match=match):
            FieldEntry.model_validate(_entry(**over))


def test_image_entry_groups_carry_rgb() -> None:
    raster = {"lo": 0.0, "hi": 1.0, "data": _b64(bytes(8))}
    group = {"label": "circle", "n": 3, "mean": raster, "rgb": {"data": _rgb(4)}}
    entry = ImageEntry.model_validate(_entry(var="pixels", groups=[group], group_by="label"))
    assert entry.cells() == 4 * 3 + 4 * 3
    with pytest.raises(ValidationError, match="rgb raster in every group"):
        ImageEntry.model_validate(
            _entry(var="pixels", groups=[{k: v for k, v in group.items() if k != "rgb"}])
        )


def test_gallery_items_are_pngs() -> None:
    item = GalleryItem(index=0, png=_b64(_png()))
    assert item.index == 0
    with pytest.raises(ValidationError, match="not base64"):
        GalleryItem(index=0, png="***")
    with pytest.raises(ValidationError, match="not a PNG"):
        GalleryItem(index=0, png=_b64(b"\xff\xd8\xff\xe0"))
    example = GalleryView.model_json_schema()["examples"][0]
    view = GalleryView.model_validate(example)
    assert view.selection == "all" and len(view.items) == view.n_images
    with pytest.raises(ValidationError, match="every image"):
        GalleryView.model_validate(example | {"n_images": 5})
    with pytest.raises(ValidationError, match="no more items"):
        GalleryView.model_validate(example | {"n_images": 2, "selection": "even"})
    dup = [example["items"][0], example["items"][0]]
    with pytest.raises(ValidationError, match="distinct"):
        GalleryView.model_validate(example | {"items": dup, "n_images": 9, "selection": "even"})
    with pytest.raises(ValidationError):
        GalleryView.model_validate(example | {"thumb_height": 65})
    assert MAX_GALLERY_ITEMS == 240


# ============================================================================ classification


def test_classification_cards_and_chooser() -> None:
    assert set(MODEL_ABOUTS) >= CLASSIFICATION_MODEL_IDS
    assert not CLASSIFICATION_MODEL_IDS & PLANNED_MODEL_IDS
    goals = {c.value for q in CHOOSER_QUESTIONS if q.id == "goal" for c in q.choices}
    shapes = {c.value for q in CHOOSER_QUESTIONS if q.id == "data_shape" for c in q.choices}
    assert "classify" in goals and "images" in shapes
    assert MODEL_ABOUTS["sklearn.multiclass_classifier"].fits["goal"][0] == "classify"
    assert MODEL_ABOUTS["tabular.image_logistic"].fits["data_shape"] == ["images"]
    assert "classify" in MODEL_ABOUTS["sklearn.tabular_classifier"].fits["goal"]
    assert "LightGBM" in (MODEL_ABOUTS["sklearn.tabular_classifier"].expert_notes or "")
    assert "ordinal" in (MODEL_ABOUTS["tabular.hierarchical_glm"].expert_notes or "")
    for plot in ("core.roc_pr", "core.confusion"):
        assert "binary or multiclass" in PLOT_ABOUTS[plot].summary_plain


# ======================================================================================= docs


def _docs() -> Path | None:
    return next(
        (p / "docs" for p in Path(__file__).resolve().parents if (p / "docs").is_dir()), None
    )


def test_reference_docs_cover_v014() -> None:
    docs = _docs()
    if docs is None or not (docs / "reference" / "http-api.md").exists():  # pragma: no cover
        pytest.skip("docs/ not available")
    text = (docs / "reference" / "http-api.md").read_text("utf-8")
    for e in V014_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            name = dto.removeprefix("Page[").removesuffix("]")
            assert f"`{name}`" in text, dto
    for dto in ("RunViewQuery", "CompareSeries", "CompareParam", "CompareMedia", "RunKeyCount"):
        assert f"`{dto}`" in text, dto
    protocol = (docs / "reference" / "run-protocol.md").read_text("utf-8")
    for needle in ("`media`", "media_prefix", "MEDIA_REJECTED", "MEDIA_INVALID"):
        assert needle in protocol, needle


# ============================================================================ run-query vectors

VECTORS = Path(__file__).parent / "data" / "run_query_vectors.json"

_REFS = [
    "name", "created_at", " name ", "metric.rmse", "metric.rmse.min", "metric.rmse.max",
    "metric.rmse.count", "metric.rmse.last", "metric.loss{split=val}",
    "metric.loss{split=val,fold=2}.min", "metric.loss{fold=2,split=val}", "metric.loss{}",
    "metric.Loss", "metric.loss/val", "metric.loss.avg", "metric.loss{split}",
    "metric.loss{split=val,split=val}", "metric.loss{Split=val}", "metric.loss{split=a b}",
    "metric.", "metric.loss{split=val", "param.lr", "param.fit.draws", "param.settings/adstock",
    "param.", "param.a b", "tag.team", "tag.mapi.source", "tag.", "tags.team", "bogus", "",
    "metric.loss.min.max", "param.x" + "y" * 250, "metric.a{k1=1,k2=2,k3=3,k4=4,k5=5,k6=6,"
    "k7=7,k8=8,k9=9}",
]  # fmt: skip
_FILTERS = [
    "metric.rmse < 3", "metric.rmse<3", "metric.rmse <= 0.5", "metric.rmse >= -1e3",
    "metric.rmse != 2", "metric.rmse = 1_000", "metric.loss{split=val}.min > 0.1",
    "metric.rmse ~ 3", "metric.rmse < abc", "metric.rmse < inf", "metric.rmse < nan",
    "metric.rmse", "metric.rmse <", "param.lr = 0.01", "param.lr < 0.01", "param.lr ~ adam",
    "param.lr != x", "param.lr >= 1e-3", "param.lr < x", "param.lr = ", "tag.team = growth",
    "tag.team ~ grow", "tag.team != a", "tag.team < 3", "name = best run", "name ~ adstock",
    "name != x", "created_at > 2026-01-01", "bogus = 1", "= 1", "metric.loss{split=v=1} < 2",
    "param.path = a<b", "tag.k = a=b", "metric.rmse =< 3", "metric.rmse !~ 3",
    "param.name = it's", "param.x = \"q\"",
]  # fmt: skip
_ORDERS = [
    "metric.rmse", "-metric.rmse", " -metric.loss{split=val}.min ", "name", "-created_at",
    "param.lr", "-tag.team", "--metric.rmse", "-", "bogus", "metric.rmse desc",
]  # fmt: skip


def _vector(kind: str, text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "input": text}
    try:
        if kind == "ref":
            out["text"] = parse_run_ref(text).text()
        elif kind == "filter":
            f = parse_run_filter(text)
            out |= {"text": f.ref.text(), "op": f.op, "value": f.value}
        else:
            o = parse_run_order(text)
            out |= {"text": o.ref.text(), "descending": o.descending}
        out["error"] = None
    except ValueError as exc:
        out["error"] = str(exc)
    return out


def run_query_vectors() -> dict[str, Any]:
    """The shared test vectors: each input with its parsed canonical form or its error."""
    vectors = [
        *(_vector("ref", t) for t in _REFS),
        *(_vector("filter", t) for t in _FILTERS),
        *(_vector("order", t) for t in _ORDERS),
    ]
    return {
        "generated_by": "packages/spec/tests/test_compare_media_contract.py "
        "(MAPI_UPDATE_SNAPSHOTS=1); do not edit",
        "vectors": vectors,
    }


def _web_copy() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        runs = parent / "apps" / "web" / "src" / "features" / "runs"
        if runs.is_dir():
            return runs / "run-query.vectors.gen.json"
    return None


def test_run_query_vectors_are_current() -> None:
    text = json.dumps(run_query_vectors(), indent=2, ensure_ascii=False) + "\n"
    web = _web_copy()
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        VECTORS.write_text(text, "utf-8")
        if web is not None:
            web.write_text(text, "utf-8")
    assert VECTORS.read_text("utf-8") == text, (
        "run-query vectors changed: regenerate with MAPI_UPDATE_SNAPSHOTS=1"
    )
    if web is not None:  # pragma: no branch - the web app is in the repository
        assert web.exists() and web.read_text("utf-8") == text, (
            "apps/web/src/features/runs/run-query.vectors.gen.json is stale: regenerate with "
            "MAPI_UPDATE_SNAPSHOTS=1"
        )
    vectors = run_query_vectors()["vectors"]
    assert {v["kind"] for v in vectors} == {"ref", "filter", "order"}
    assert any(v["error"] for v in vectors) and any(v["error"] is None for v in vectors)
