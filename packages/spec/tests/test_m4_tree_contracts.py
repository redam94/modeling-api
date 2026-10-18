"""M4 contracts: tree schemas, node selection, HDF5 mappings, multi-file assemblies, upload
inspection, the dataset-version DTOs and static validation of ``data.nodes``."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec import Capabilities, Catalog, DataSchema, ModelSpec
from mapi_spec.api import (
    ALL_ENDPOINTS,
    ENDPOINTS,
    M4_ENDPOINTS,
    ROUTE_ACCESS,
    DatasetVersion,
    DatasetVersionCreate,
    RouteScope,
)
from mapi_spec.errors import ERROR_INFO, ErrorCode, error_for_code
from mapi_spec.examples import E33_RAW, E33_YAML
from mapi_spec.executor import RunKind
from mapi_spec.explore import DataExploreRequest
from mapi_spec.ingest import (
    AssemblySource,
    Hdf5Coord,
    Hdf5Mapping,
    Hdf5Var,
    IngestAssembly,
    IngestMapping,
    InspectedArray,
    UploadInspection,
)
from mapi_spec.jobs import (
    DATATREE_OUTPUT,
    INSPECT_OUTPUT,
    TREE_DATASET_OUTPUTS,
    TREE_SCHEMA_OUTPUT,
    IngestJob,
    InspectJob,
    parse_job,
    upload_input,
)
from mapi_spec.loader import canonical_json, parse_yaml, spec_hash
from mapi_spec.quotas import PriorityClass, default_priority
from mapi_spec.spec import DataBlock, NodeSelect, SideTable
from mapi_spec.tree import (
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    NodeSelectionError,
    TreeSchema,
    is_ancestor,
    node_ancestors,
    node_depth,
    node_join,
    node_name,
    node_parent,
)
from mapi_spec.validate import resolve_input_schema, validate_spec
from pydantic import TypeAdapter, ValidationError

pytestmark = pytest.mark.unit

_UPL = "upl_01jaf3d1e2f3g4h5j6k7m8n9p0"
_UPL2 = "upl_01jaf3d1e2f3g4h5j6k7m8n9p1"
_UPL3 = "upl_01jaf3d1e2f3g4h5j6k7m8n9p2"

_IDX = {"kind": "index"}
_DATE = {"kind": "time", "policy": "extend", "freq": "D"}


def _var(*dims: str, **kw: Any) -> dict[str, Any]:
    return {"dims": list(dims), "dtype": "float64", **kw}


def _tree(nodes: dict[str, Any]) -> TreeSchema:
    return TreeSchema.model_validate({"nodes": nodes})


def _e33_tree() -> TreeSchema:
    """E33_RAW split over two nodes below an empty root."""
    dims = E33_RAW["dims"]
    return _tree(
        {
            "/": {},
            "/sales": {
                "dims": {"market": dims["market"], "date": dims["date"]},
                "vars": {"revenue_local": E33_RAW["vars"]["revenue_local"]},
            },
            "/media": {
                "dims": dims,
                "vars": {"spend_local": E33_RAW["vars"]["spend_local"]},
            },
        }
    )


# ================================================================================= NodePath

_NODE = TypeAdapter(TreeSchema.model_fields["nodes"].annotation)


@pytest.mark.parametrize(
    "path", ["/", "/a", "/instrument/psf", "/_x/y.z-1", "/a/b/c/d/e/f/g/h", "/" + "a" * 64]
)
def test_node_paths_accepted(path: str) -> None:
    assert list(_NODE.validate_python({path: {}})) == [path]


@pytest.mark.parametrize(
    "path",
    [
        "",
        "a",
        "/a/",
        "//a",
        "/1a",
        "/a b",
        "/a/../b",
        "/a/b/c/d/e/f/g/h/i",  # depth 9
        "/" + "a" * 65,
        "/a\x00",
    ],
)
def test_node_paths_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        _NODE.validate_python({path: {}})


def test_node_helpers() -> None:
    assert node_parent("/") is None
    assert node_parent("/a") == "/"
    assert node_parent("/a/b") == "/a"
    assert node_name("/") == "" and node_name("/a/b") == "b"
    assert node_depth("/") == 0 and node_depth("/a/b") == 2
    assert node_ancestors("/a/b/c") == ["/a/b", "/a", "/"]
    assert node_ancestors("/") == []
    assert is_ancestor("/", "/a") and is_ancestor("/a", "/a/b")
    assert not is_ancestor("/a", "/a") and not is_ancestor("/a", "/ab")
    assert not is_ancestor("/a/b", "/a")
    assert node_join("/", "/psf") == "/psf"
    assert node_join("/instrument", "/psf") == "/instrument/psf"
    assert node_join("/instrument", "/") == "/instrument"
    with pytest.raises(ValueError, match="deeper"):
        node_join("/a/b/c/d/e", "/f/g/h/i")
    assert MAX_TREE_DEPTH == 8 and MAX_TREE_NODES == 256


# =============================================================================== TreeSchema


def test_tree_schema_round_trips_and_helpers() -> None:
    t = _e33_tree()
    again = TreeSchema.model_validate_json(t.model_dump_json())
    assert again == t
    assert t.root == DataSchema()
    assert t.children("/") == ["/sales", "/media"]
    assert t.default_node() == "/sales"
    rooted = _tree({"/": {"dims": {"t": _IDX}, "vars": {"y": _var("t")}}})
    assert rooted.default_node() == "/"
    assert _tree({"/": {}}).default_node() == "/"
    assert _tree({"/a": {}, "/": {}}).children("/a") == []


def test_tree_schema_needs_ancestors() -> None:
    with pytest.raises(ValidationError, match="misses its ancestor"):
        _tree({"/": {}, "/a/b": {}})
    with pytest.raises(ValidationError, match="misses its ancestor"):
        _tree({"/a": {}})
    with pytest.raises(ValidationError):
        _tree({})


def test_tree_schema_inherited_dims_must_be_equal() -> None:
    _tree(
        {
            "/": {"dims": {"t": _IDX}},
            "/a": {"dims": {"t": _IDX}, "vars": {"y": _var("t")}},
            "/a/b": {"dims": {"t": _IDX, "u": _IDX}, "vars": {"z": _var("t", "u")}},
        }
    )
    with pytest.raises(ValidationError, match="differs from the same dim of its ancestor '/'"):
        _tree({"/": {"dims": {"t": _IDX}}, "/a": {"dims": {"t": {"kind": "integer"}}}})
    with pytest.raises(ValidationError, match="ancestor '/a'"):
        _tree(
            {
                "/": {},
                "/a": {"dims": {"t": _IDX}},
                "/a/b": {"dims": {"t": {"kind": "categorical"}}},
            }
        )
    # siblings may use the same dim name differently (they do not see each other)
    _tree({"/": {}, "/a": {"dims": {"t": _IDX}}, "/b": {"dims": {"t": {"kind": "float"}}}})


def test_tree_schema_node_limit() -> None:
    nodes = {"/": {}} | {f"/n{i}": {} for i in range(MAX_TREE_NODES - 1)}
    _tree(nodes)
    with pytest.raises(ValidationError):
        _tree(nodes | {"/extra": {}})


# ===================================================================================== flat


def _sel(**entries: dict[str, Any]) -> dict[str, NodeSelect]:
    return {k: NodeSelect.model_validate(v) for k, v in entries.items()}


def test_flat_merges_nodes() -> None:
    flat = _e33_tree().flat(_sel(sales={"path": "/sales"}, media={"path": "/media"}))
    assert flat == DataSchema.model_validate(
        {
            "dims": {
                "market": E33_RAW["dims"]["market"],
                "date": E33_RAW["dims"]["date"],
                "channel": E33_RAW["dims"]["channel"],
            },
            "vars": E33_RAW["vars"],
        }
    )
    assert set(flat.dims) == set(E33_RAW["dims"])


def test_flat_prefix_and_vars() -> None:
    t = _tree(
        {
            "/": {},
            "/a": {"dims": {"t": _IDX, "k": _IDX}, "vars": {"x": _var("t"), "w": _var("k")}},
            "/b": {"dims": {"t": _IDX}, "vars": {"x": _var("t", units="EUR")}},
        }
    )
    flat = t.flat(_sel(a={"path": "/a", "vars": ["x"]}, b={"path": "/b", "prefix": "b_"}))
    assert list(flat.vars) == ["x", "b_x"]
    assert flat.vars["b_x"].units == "EUR"
    assert list(flat.dims) == ["t"]  # 'k' is only used by the unselected 'w'
    whole = t.flat(_sel(a={"path": "/a"}))
    assert set(whole.dims) == {"t", "k"} and set(whole.vars) == {"x", "w"}


def test_flat_conflicts() -> None:
    t = _tree(
        {
            "/": {},
            "/a": {"dims": {"t": _IDX}, "vars": {"x": _var("t")}},
            "/b": {"dims": {"t": _IDX}, "vars": {"x": _var("t")}},
            "/c": {"dims": {"t": {"kind": "integer"}}, "vars": {"y": _var("t")}},
            "/d": {"dims": {"x": _IDX}, "vars": {"z": _var("x")}},
            "/long": {"layout": "long", "dims": {"t": _IDX}, "vars": {"q": _var("t")}},
        }
    )
    with pytest.raises(NodeSelectionError, match="give one of them a prefix") as exc:
        t.flat(_sel(a={"path": "/a"}, b={"path": "/b"}))
    assert exc.value.key == "b" and exc.value.name == "x"
    with pytest.raises(NodeSelectionError, match="differs from the same dim") as exc:
        t.flat(_sel(a={"path": "/a"}, c={"path": "/c"}))
    assert exc.value.key == "c" and exc.value.name == "t"
    with pytest.raises(NodeSelectionError, match="name of a variable") as exc:
        t.flat(_sel(a={"path": "/a"}, d={"path": "/d"}))  # var 'x', then dim 'x'
    assert exc.value.key == "d"
    with pytest.raises(NodeSelectionError, match="also selected") as exc:
        t.flat(_sel(d={"path": "/d"}, a={"path": "/a"}))  # dim 'x', then var 'x'
    with pytest.raises(NodeSelectionError, match="no node '/zz'") as exc:
        t.flat(_sel(z={"path": "/zz"}))
    assert exc.value.key == "z"
    with pytest.raises(NodeSelectionError, match="no variable"):
        t.flat(_sel(a={"path": "/a", "vars": ["nope"]}))
    with pytest.raises(NodeSelectionError, match="mix dense and long"):
        t.flat(_sel(a={"path": "/a"}, q={"path": "/long"}))
    with pytest.raises(NodeSelectionError, match="hold no variables"):
        t.flat(_sel(root={"path": "/"}))
    with pytest.raises(NodeSelectionError, match="at least one"):
        t.flat({})


def test_flat_keeps_ragged_specs_it_can() -> None:
    t = _tree(
        {
            "/": {},
            "/r": {
                "dims": {"hh": _IDX, "trip": _IDX},
                "vars": {
                    "dist": _var("hh", "trip"),
                    "m": {"dims": ["hh", "trip"], "dtype": "bool"},
                },
                "ragged": [
                    {"group_dim": "hh", "within_dim": "trip", "form": "padded", "mask_var": "m"}
                ],
            },
        }
    )
    flat = t.flat(_sel(r={"path": "/r", "prefix": "p_"}))
    assert flat.ragged[0].mask_var == "p_m"
    # without its mask variable the ragged spec cannot be kept
    assert t.flat(_sel(r={"path": "/r", "vars": ["dist"]})).ragged == []


def test_node_select_rules() -> None:
    with pytest.raises(ValidationError):
        NodeSelect.model_validate({"path": "/a", "vars": ["x", "x"]})
    with pytest.raises(ValidationError):
        NodeSelect.model_validate({"path": "/a", "vars": []})
    with pytest.raises(ValidationError):
        NodeSelect.model_validate({"path": "/a", "prefix": "1x"})
    with pytest.raises(ValidationError):
        NodeSelect.model_validate({"path": "/a", "prefix": "a" * 33})
    with pytest.raises(ValidationError):
        NodeSelect.model_validate({"path": "a"})


# ===================================================================================== HDF5


def test_hdf5_var_rules() -> None:
    v = Hdf5Var.model_validate({"path": "/g/x", "name": "x", "dims": ["a", "b"]})
    assert v.node == "/"
    for bad in (
        {"path": "g/x", "name": "x"},  # not absolute
        {"path": "/", "name": "x"},  # no object
        {"path": "/a\x00b", "name": "x"},
        {"path": "/x", "name": "x", "dims": ["a", "a"]},
        {"path": "/x", "name": "1x"},
        {"path": "/x", "name": "x", "node": "psf"},
    ):
        with pytest.raises(ValidationError):
            Hdf5Var.model_validate(bad)


def test_hdf5_coord_sources() -> None:
    assert Hdf5Coord(dim="t").path is None  # neither: an integer index
    assert Hdf5Coord(dim="t", path="/axes/t").path == "/axes/t"
    assert Hdf5Coord(dim="t", start=0, step=0.5).step == 0.5
    assert Hdf5Coord(dim="t", start="2024-01-01", step="W-MON").start == "2024-01-01"
    assert isinstance(Hdf5Coord(dim="t", start=3).start, int)
    with pytest.raises(ValidationError, match="not both"):
        Hdf5Coord(dim="t", path="/axes/t", start=0)
    with pytest.raises(ValidationError, match="step needs start"):
        Hdf5Coord(dim="t", step=1)
    with pytest.raises(ValidationError, match="boolean"):
        Hdf5Coord.model_validate({"dim": "t", "start": True})


def _h5(vars_: list[dict[str, Any]], coords: list[dict[str, Any]] | None = None) -> Hdf5Mapping:
    return Hdf5Mapping.model_validate({"vars": vars_, "coords": coords or []})


def test_hdf5_mapping_rules() -> None:
    m = _h5(
        [
            {"path": "/img", "name": "image", "dims": ["row", "col"]},
            {"path": "/psf/k", "node": "/psf", "name": "image", "dims": ["row", "col"]},
        ],
        [{"dim": "row", "start": 0, "step": 1}, {"dim": "col", "node": "/psf"}],
    )
    assert len(m.vars) == 2  # the same name in two nodes is fine
    with pytest.raises(ValidationError, match="mapped twice"):
        _h5([{"path": "/a", "name": "x"}, {"path": "/b", "name": "x"}])
    with pytest.raises(ValidationError, match="coords given twice"):
        _h5([{"path": "/a", "name": "x", "dims": ["t"]}], [{"dim": "t"}, {"dim": "t"}])
    with pytest.raises(ValidationError, match="name of one of its dims"):
        _h5([{"path": "/a", "name": "t", "dims": ["t"]}])
    with pytest.raises(ValidationError, match="both as dim and variable"):
        _h5([{"path": "/a", "name": "x", "dims": ["t"]}, {"path": "/b", "name": "t"}])
    with pytest.raises(ValidationError, match="not a dim of any variable"):
        _h5([{"path": "/a", "name": "x", "dims": ["t"]}], [{"dim": "u"}])
    with pytest.raises(ValidationError, match="not a dim of any variable"):
        # a coord of a child node does not serve its parent
        _h5([{"path": "/a", "name": "x", "dims": ["t"]}], [{"dim": "t", "node": "/c"}])
    with pytest.raises(ValidationError):
        Hdf5Mapping.model_validate({"vars": []})


def test_ingest_mapping_hdf5_rule() -> None:
    h5 = {"vars": [{"path": "/a", "name": "x"}]}
    m = IngestMapping.model_validate({"format": "hdf5", "hdf5": h5})
    assert m.hdf5 is not None
    with pytest.raises(ValidationError, match="needs 'hdf5'"):
        IngestMapping.model_validate({"format": "hdf5"})
    with pytest.raises(ValidationError, match="only used with format 'hdf5'"):
        IngestMapping.model_validate({"format": "netcdf", "hdf5": h5})
    with pytest.raises(ValidationError, match="not dims/vars"):
        IngestMapping.model_validate({"format": "hdf5", "hdf5": h5, "vars": {"x": {"column": "x"}}})
    with pytest.raises(ValidationError, match="preset"):
        IngestMapping.model_validate({"format": "hdf5", "hdf5": h5, "preset": "mff"})
    # unchanged: self-describing formats keep their optional rename/select
    assert IngestMapping(format="netcdf").model_dump(mode="json") == {
        "format": "netcdf",
        "layout": "dense",
        "dims": {},
        "vars": {},
    }


# ================================================================================= assembly


def _src(upload_id: str = _UPL, **kw: Any) -> dict[str, Any]:
    return {"upload_id": upload_id, "mapping": {"format": "netcdf"}, **kw}


def _asm(*sources: dict[str, Any]) -> IngestAssembly:
    return IngestAssembly.model_validate({"sources": list(sources)})


def test_assembly_source_rules() -> None:
    s = AssemblySource.model_validate(_src())
    assert (s.node, s.combine, s.join) == ("/", "node", "exact")
    with pytest.raises(ValidationError, match="needs concat_dim and concat_label"):
        AssemblySource.model_validate(_src(combine="concat", concat_dim="year"))
    with pytest.raises(ValidationError, match="needs concat_dim and concat_label"):
        AssemblySource.model_validate(_src(combine="concat", concat_label=1))
    with pytest.raises(ValidationError, match="only used by combine 'concat'"):
        AssemblySource.model_validate(_src(concat_dim="year"))
    with pytest.raises(ValidationError, match="only used by combine 'concat'"):
        AssemblySource.model_validate(_src(combine="merge", concat_label=1))
    with pytest.raises(ValidationError, match="boolean"):
        AssemblySource.model_validate(_src(combine="concat", concat_dim="year", concat_label=True))
    with pytest.raises(ValidationError):
        AssemblySource.model_validate(_src(upload_id="../x"))


def test_assembly_combines_per_node() -> None:
    a = _asm(
        _src(_UPL, combine="concat", concat_dim="year", concat_label=2024),
        _src(_UPL2, combine="concat", concat_dim="year", concat_label=2025),
        _src(_UPL3, node="/instrument"),
    )
    assert a.nodes() == {"/": [0, 1], "/instrument": [2]}
    merged = _asm(
        _src(_UPL, combine="merge", join="outer"), _src(_UPL2, combine="merge", join="outer")
    )
    assert len(merged.sources) == 2
    assert len(_asm(_src(_UPL, combine="merge")).sources) == 1  # merge of one source
    assert len(_asm(_src(_UPL, combine="concat", concat_dim="y", concat_label="a")).sources) == 1


@pytest.mark.parametrize(
    ("sources", "match"),
    [
        ([_src(_UPL), _src(_UPL, node="/b")], "used once"),
        ([_src(_UPL), _src(_UPL2)], "combine them"),
        (
            [
                _src(_UPL, combine="merge"),
                _src(_UPL2, combine="concat", concat_dim="y", concat_label=1),
            ],
            "different ways",
        ),
        (
            [
                _src(_UPL, combine="concat", concat_dim="y", concat_label=1),
                _src(_UPL2, combine="concat", concat_dim="z", concat_label=2),
            ],
            "same concat_dim",
        ),
        (
            [
                _src(_UPL, combine="concat", concat_dim="y", concat_label=1),
                _src(_UPL2, combine="concat", concat_dim="y", concat_label=1),
            ],
            "labels must be unique",
        ),
        (
            [
                _src(_UPL, combine="concat", concat_dim="y", concat_label=1),
                _src(_UPL2, combine="concat", concat_dim="y", concat_label="1"),
            ],
            "mix numbers and strings",
        ),
        (
            [_src(_UPL, combine="merge"), _src(_UPL2, combine="merge", join="inner")],
            "same join",
        ),
    ],
)
def test_assembly_rules(sources: list[dict[str, Any]], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _asm(*sources)


def test_assembly_size_limits() -> None:
    with pytest.raises(ValidationError):
        _asm()
    many = [_src(f"upl_{i:026d}", node=f"/n{i}") for i in range(65)]
    with pytest.raises(ValidationError):
        _asm(*many)
    assert len(_asm(*many[:64]).sources) == 64


# ================================================================================== jobs


def test_ingest_job_one_of_and_limits() -> None:
    asm = _asm(_src())
    job = IngestJob(assembly=asm)
    assert (job.max_nodes, job.max_sources, job.max_hdf5_objects) == (256, 64, 10_000)
    assert (job.max_ndim, job.max_attr_bytes) == (32, 65_536)
    # defaults stay out of the wire form, so older runners keep reading single-file jobs
    single = IngestJob(mapping=IngestMapping(format="netcdf")).model_dump(mode="json")
    assert not {"assembly", "max_nodes", "max_sources", "max_ndim"} & set(single)
    with pytest.raises(ValidationError, match="exactly one"):
        IngestJob(mapping=IngestMapping(format="netcdf"), assembly=asm)
    with pytest.raises(ValidationError, match="exactly one"):
        IngestJob()
    with pytest.raises(ValidationError, match="max_sources"):
        IngestJob(assembly=_asm(_src(_UPL), _src(_UPL2, node="/b")), max_sources=1)
    parsed = parse_job(RunKind.INGEST, job.model_dump(mode="json"))
    assert parsed == job
    assert upload_input(0) == "upload.0" and upload_input(63) == "upload.63"
    with pytest.raises(ValueError):
        upload_input(64)


def test_output_names() -> None:
    assert DATATREE_OUTPUT == "data.tree.zip"
    assert TREE_SCHEMA_OUTPUT == "tree_schema.json"
    assert INSPECT_OUTPUT == "inspection.json"
    assert TREE_DATASET_OUTPUTS[:3] == ("data.tree.zip", "tree_schema.json", "schema.json")


def test_inspect_kind_and_job() -> None:
    assert RunKind("inspect") is RunKind.INSPECT and not RunKind.INSPECT.is_parent
    assert default_priority(RunKind.INSPECT) is PriorityClass.INTERACTIVE
    job = parse_job(RunKind.INSPECT, {"format": "hdf5"})
    assert isinstance(job, InspectJob) and job.max_arrays == 2000
    assert parse_job(RunKind.INSPECT, {}) == InspectJob()
    with pytest.raises(Exception, match="spec is invalid"):
        parse_job(RunKind.INSPECT, {"format": "xlsx"})


def test_upload_inspection_round_trip() -> None:
    ex = UploadInspection.model_json_schema()["examples"][0]
    ins = UploadInspection.model_validate(ex)
    assert UploadInspection.model_validate_json(ins.model_dump_json()) == ins
    # responses ignore unknown fields (a newer runner may add some)
    assert UploadInspection.model_validate({**ex, "extra": 1}) == ins
    arr = InspectedArray(path="/x", shape=[2, 3], dtype="int8", attrs={"a": None, "b": 1.5})
    assert arr.filters == [] and arr.dims_hint is None
    with pytest.raises(ValidationError):
        UploadInspection.model_validate(
            {**ex, "arrays": [{"path": f"/a{i}", "dtype": "f8"} for i in range(2001)]}
        )
    with pytest.raises(ValidationError):
        InspectedArray(path="/x", dtype="f8", attrs={f"k{i}": i for i in range(65)})


# =================================================================================== API


def test_dataset_version_create_exactly_one() -> None:
    single = DatasetVersionCreate(upload_id=_UPL, mapping=IngestMapping(format="zarr_zip"))
    assert single.upload_ids == [_UPL]
    assert "assembly" not in single.model_dump(mode="json")
    multi = DatasetVersionCreate(assembly=_asm(_src(_UPL), _src(_UPL2, node="/b")))
    assert multi.upload_ids == [_UPL, _UPL2]
    with pytest.raises(ValidationError, match="not both"):
        DatasetVersionCreate(
            upload_id=_UPL, mapping=IngestMapping(format="zarr_zip"), assembly=_asm(_src())
        )
    with pytest.raises(ValidationError, match="not both"):
        DatasetVersionCreate(upload_id=_UPL, assembly=_asm(_src()))
    with pytest.raises(ValidationError, match="give upload_id and mapping"):
        DatasetVersionCreate(upload_id=_UPL)
    with pytest.raises(ValidationError, match="give upload_id and mapping"):
        DatasetVersionCreate()
    for ex in DatasetVersionCreate.model_json_schema()["examples"]:
        dto = DatasetVersionCreate.model_validate(ex)
        assert DatasetVersionCreate.model_validate_json(dto.model_dump_json()) == dto


def test_dataset_version_layout_fields() -> None:
    old, tree = DatasetVersion.model_json_schema()["examples"]
    legacy = {k: v for k, v in old.items() if k != "sources"}
    dv = DatasetVersion.model_validate(legacy)  # a pre-M4 server response
    assert (dv.layout, dv.tree_schema, dv.sources) == ("dataset", None, [])
    tv = DatasetVersion.model_validate(tree)
    assert tv.layout == "tree" and tv.tree_schema is not None
    assert tv.tree_schema.default_node() == "/observations"
    assert DatasetVersion.model_validate_json(tv.model_dump_json()) == tv
    with pytest.raises(ValidationError):
        DatasetVersion.model_validate({**tree, "layout": "forest"})


def test_m4_endpoints_and_access() -> None:
    assert ALL_ENDPOINTS == ENDPOINTS + M4_ENDPOINTS
    by_op = {e.operation_id: e for e in M4_ENDPOINTS}
    post, get = by_op["inspect_upload"], by_op["get_upload_inspection"]
    assert (post.method, post.success, post.response) == ("POST", 202, "Run")
    assert post.path == "/v1/datasets/{dataset_id}/uploads/{upload_id}/inspect"
    assert (get.method, get.success, get.response) == ("GET", 200, "UploadInspection")
    assert ErrorCode.NOT_READY in get.errors and ErrorCode.NOT_FOUND in get.errors
    for op in by_op:
        assert ROUTE_ACCESS[op].scope is RouteScope.PROJECT
    assert ROUTE_ACCESS["inspect_upload"].action.value == "update"  # type: ignore[union-attr]
    assert ROUTE_ACCESS["get_upload_inspection"].action.value == "read"  # type: ignore[union-attr]


def test_m4_error_codes() -> None:
    assert ERROR_INFO[ErrorCode.NOT_READY].status == 409
    assert ERROR_INFO[ErrorCode.NOT_READY].retryable
    assert ERROR_INFO[ErrorCode.INGEST_UNSAFE_FILE].status == 422
    err = error_for_code(ErrorCode.INGEST_UNSAFE_FILE, "external link")
    assert type(err).__name__ == "UnsafeUpload" and err.code is ErrorCode.INGEST_UNSAFE_FILE


def test_explore_request_node() -> None:
    assert DataExploreRequest().node is None
    assert "node" not in DataExploreRequest().model_dump(mode="json")
    assert DataExploreRequest(node="/psf").node == "/psf"
    with pytest.raises(ValidationError):
        DataExploreRequest(node="psf")


def test_capabilities_input() -> None:
    base = {"inference": "mcmc", "representation": "draws", "score_modes": ["predict"]}
    caps = Capabilities.model_validate(base)
    assert caps.input == "dataset" and "input" not in caps.model_dump(mode="json")
    assert Capabilities.model_validate({**base, "input": "tree"}).input == "tree"
    with pytest.raises(ValidationError):
        Capabilities.model_validate({**base, "input": "graph"})


# ================================================================================ specs


def _e33() -> dict[str, Any]:
    data = parse_yaml(E33_YAML)
    assert isinstance(data, dict)
    return data


def _with_nodes(nodes: dict[str, Any] | None, **data: Any) -> ModelSpec:
    doc = copy.deepcopy(_e33())
    if nodes is not None:
        doc["data"]["nodes"] = nodes
    doc["data"].update(data)
    return ModelSpec.model_validate(doc)


def test_data_block_fields_keep_old_specs_identical() -> None:
    plain = ModelSpec.model_validate(_e33())
    assert plain.data.nodes is None and plain.data.join == "exact"
    assert "nodes" not in canonical_json(plain) and '"join"' not in canonical_json(plain)
    assert spec_hash(plain) == spec_hash(ModelSpec.model_validate(_e33()))
    spec = _with_nodes({"sales": {"path": "/sales"}}, join="outer")
    assert spec.data.nodes is not None and spec.data.join == "outer"
    assert ModelSpec.model_validate_json(spec.model_dump_json(by_alias=True)) == spec
    with pytest.raises(ValidationError, match=r"only used with data\.nodes"):
        _with_nodes(None, join="inner")
    with pytest.raises(ValidationError):
        _with_nodes({})
    with pytest.raises(ValidationError):
        DataBlock.model_validate(
            {"dataset": "dataset://p/d", "nodes": {f"n{i}": {"path": "/"} for i in range(65)}}
        )


def test_side_table_node() -> None:
    st = SideTable.model_validate({"ref": "dataset://p/fx@v1", "node": "/rates"})
    assert st.node == "/rates"
    assert "node" not in SideTable.model_validate({"ref": "dataset://p/fx@v1"}).model_dump()
    with pytest.raises(ValidationError):
        SideTable.model_validate({"ref": "dataset://p/fx@v1", "node": "rates"})


def test_validate_tree_dataset_with_nodes(catalog: Catalog) -> None:
    spec = _with_nodes({"sales": {"path": "/sales"}, "media": {"path": "/media"}})
    res = validate_spec(spec, catalog, DataSchema(), tree_schema=_e33_tree())
    assert res.ok, res.issues
    plain = validate_spec(
        ModelSpec.model_validate(_e33()), catalog, DataSchema.model_validate(E33_RAW)
    )
    assert res.stages[-1].schema == plain.stages[-1].schema  # same pipeline input, same result


def test_validate_existing_callers_unchanged(catalog: Catalog) -> None:
    res = validate_spec(
        ModelSpec.model_validate(_e33()), catalog, DataSchema.model_validate(E33_RAW)
    )
    assert res.ok and res.issues == []


def test_validate_tree_issues(catalog: Catalog) -> None:
    tree = _e33_tree()
    # no data.nodes and an empty root: pick nodes
    res = validate_spec(ModelSpec.model_validate(_e33()), catalog, DataSchema(), tree_schema=tree)
    issue = res.issues[0]
    assert (issue.code, issue.pointer) == (ErrorCode.SCHEMA_INVALID, "/data")
    assert "pick nodes in data.nodes" in issue.message and "'/sales'" in (
        issue.suggested_action or ""
    )
    assert not res.ok and res.stages[0].schema is None
    # an unknown node: the issue points at its data.nodes entry
    spec = _with_nodes({"sales": {"path": "/sales"}, "media": {"path": "/nope"}})
    res = validate_spec(spec, catalog, DataSchema(), tree_schema=tree)
    assert res.issues[0].pointer == "/data/nodes/media" and not res.ok
    # a prefix renames the variable the binding names: the binding reports it
    spec = _with_nodes({"sales": {"path": "/sales"}, "media": {"path": "/media", "prefix": "m_"}})
    res = validate_spec(spec, catalog, DataSchema(), tree_schema=tree)
    assert res.errors[0].code is ErrorCode.BINDING_INVALID
    assert res.errors[0].pointer == "/data/bind/vars/spend"
    # data.nodes on a plain dataset
    spec = _with_nodes({"sales": {"path": "/sales"}})
    res = validate_spec(spec, catalog, DataSchema.model_validate(E33_RAW))
    assert res.issues[0].pointer == "/data/nodes" and not res.ok
    assert "not a tree" in res.issues[0].message


def test_resolve_input_schema_root_and_whole_selection() -> None:
    raw = DataSchema.model_validate(E33_RAW)
    rooted = TreeSchema(nodes={"/": raw, "/psf": DataSchema()})
    spec = ModelSpec.model_validate(_e33())
    assert resolve_input_schema(spec, DataSchema(), rooted) == (raw, [])
    assert resolve_input_schema(spec, raw) == (raw, [])
    flat, issues = resolve_input_schema(
        _with_nodes({"a": {"path": "/"}, "b": {"path": "/"}}), DataSchema(), rooted
    )
    assert flat is None and issues[0].pointer == "/data/nodes/b"


def test_reference_docs_cover_m4() -> None:
    from pathlib import Path

    docs = next(
        (p / "docs" for p in Path(__file__).resolve().parents if (p / "docs").is_dir()), None
    )
    if docs is None or not (docs / "reference" / "http-api.md").exists():  # pragma: no cover
        pytest.skip("docs/ not available")
    text = (docs / "reference" / "http-api.md").read_text("utf-8")
    for e in M4_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            assert f"`{dto}`" in text, dto
    for dto in ("TreeSchema", "IngestAssembly", "Hdf5Mapping", "InspectedArray"):
        assert f"`{dto}`" in text, dto
