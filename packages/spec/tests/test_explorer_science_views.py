"""The scientific Data Explorer views: the example documents validate, the shapes and caps
hold, and the index and request carry the new optional fields."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mapi_spec.explore import DataExploreRequest, StructureHints
from mapi_spec.explorer_examples import SCIENCE_VIEW_EXAMPLES
from mapi_spec.views import (
    DATA_SECTIONS,
    MAX_FIELD_CELLS,
    MAX_MAP_VERTICES,
    SCIENCE_SECTIONS,
    CircularView,
    DataOverviewView,
    ExplorerIndex,
    ImagesView,
    MapView,
    NetworkView,
    PointsView,
    ProfilesView,
    StructureView,
    TreeView,
    explore_view_name,
)
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit

DATA = Path(__file__).parent / "data" / "explorer_science"
VIEWS: dict[str, type[BaseModel]] = {
    "structure": StructureView,
    "images": ImagesView,
    "profiles": ProfilesView,
    "map": MapView,
    "network": NetworkView,
    "tree": TreeView,
    "circular": CircularView,
    "points": PointsView,
}


def _doc(section: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((DATA / f"{section}.json").read_text())
    return doc


def test_the_science_sections_are_data_sections() -> None:
    assert len(DATA_SECTIONS) == 21 and DATA_SECTIONS[-8:] == SCIENCE_SECTIONS
    assert set(VIEWS) == set(SCIENCE_SECTIONS)
    for s in SCIENCE_SECTIONS:
        assert explore_view_name(s) == f"ui/explore/{s}.json"


@pytest.mark.parametrize("section", SCIENCE_SECTIONS)
def test_every_example_validates(section: str) -> None:
    cls = VIEWS[section]
    view = cls.model_validate(_doc(section))
    assert view.format == f"mapi.data-explorer.{section}/v1"  # type: ignore[attr-defined]
    schema_example = cls.model_json_schema()["examples"][0]
    assert cls.model_validate(schema_example) == cls.model_validate(SCIENCE_VIEW_EXAMPLES[section])
    assert section in (DATA / f"{section}.json").name


def test_images_check_raster_sizes_and_the_cell_budget() -> None:
    doc = _doc("images")
    bad = json.loads(json.dumps(doc))
    bad["fields"][0]["mask"] = bad["fields"][1]["frames"][0]["mean"]  # 4 cells, not 6
    with pytest.raises(ValidationError, match="cells"):
        ImagesView.model_validate(bad)
    view = ImagesView.model_validate(doc)
    first = view.fields[0]
    # mean of 2 frames + mask + 1 group mean, 2 x 3 cells each
    assert first.cells() == 6 * 4
    assert sum(f.cells() for f in view.fields) <= MAX_FIELD_CELLS
    stats = json.loads(json.dumps(doc))
    stats["fields"][0]["frame_stats"]["mean"].append(1.0)
    with pytest.raises(ValidationError, match="frame_stats"):
        ImagesView.model_validate(stats)


def test_map_checks_rings_layers_and_edges() -> None:
    doc = _doc("map")
    for change, match in (
        (lambda d: d["layers"][0]["values"].append(1), "layer"),
        (lambda d: d["edges"].append([0, 7]), "edge"),
        (lambda d: d["rings"][0].update(area=9), "ring"),
        (lambda d: d["rings"][0]["y"].pop(), "ring"),
    ):
        bad = json.loads(json.dumps(doc))
        change(bad)
        with pytest.raises(ValidationError, match=match):
            MapView.model_validate(bad)
    big = json.loads(json.dumps(doc))
    big["rings"] = [{"area": 0, "x": [0.0] * 10_001, "y": [0.0] * 10_001}] * 2
    with pytest.raises(ValidationError, match=str(MAX_MAP_VERTICES)):
        MapView.model_validate(big)


def test_network_tree_circular_and_points_check_their_lists() -> None:
    net = _doc("network")
    net["items"][0]["matrix"] = net["items"][0]["matrix"][:3]
    with pytest.raises(ValidationError, match="square"):
        NetworkView.model_validate(net)
    net = _doc("network")
    net["items"][0]["edges"].append({"i": 0, "j": 9, "w": 1})
    with pytest.raises(ValidationError, match="not shown"):
        NetworkView.model_validate(net)
    tree = _doc("tree")
    tree["traits"][0]["values"].pop()
    with pytest.raises(ValidationError, match="trait"):
        TreeView.model_validate(tree)
    tree = _doc("tree")
    tree["n_nodes"] = 6
    with pytest.raises(ValidationError, match="one entry per node"):
        TreeView.model_validate(tree)
    circ = _doc("circular")
    circ["items"][0]["groups"][0]["counts"].append(3)
    with pytest.raises(ValidationError, match="counts"):
        CircularView.model_validate(circ)
    pts = _doc("points")
    pts["srgb"][0] = "red; background:url(x)"
    with pytest.raises(ValidationError):
        PointsView.model_validate(pts)
    pts = _doc("points")
    pts["srgb"][0] = "#FFFFFF"  # lower-case hex only
    with pytest.raises(ValidationError):
        PointsView.model_validate(pts)
    pts = _doc("points")
    pts["colour"][0]["values"].pop()
    with pytest.raises(ValidationError, match="colour"):
        PointsView.model_validate(pts)


def test_structure_names_known_sections_only() -> None:
    doc = _doc("structure")
    doc["recommended"].append("telepathy")
    with pytest.raises(ValidationError, match="unknown section"):
        StructureView.model_validate(doc)


def test_index_overview_and_request_take_the_new_optional_fields() -> None:
    examples = ExplorerIndex.model_json_schema()["examples"]
    old, new = (ExplorerIndex.model_validate(e) for e in examples)
    assert old.order is None and old.shapes is None
    assert new.shapes == ["image", "timeseries"] and new.order and new.order[1] == "structure"
    assert new.sections[-1].reason == "not_applicable"
    with pytest.raises(ValidationError):
        ExplorerIndex.model_validate({**examples[1], "shapes": ["hologram"]})
    ov = DataOverviewView(n_cells=1, summary_plain="x")
    assert "shapes" not in ov.model_dump(exclude_none=True)
    req = DataExploreRequest()
    assert req.sections == list(DATA_SECTIONS) and "hints" not in req.model_dump()
    DataExploreRequest(sections=list(DATA_SECTIONS))
    hinted = DataExploreRequest.model_validate(
        {"hints": {"y_dim": "row", "x_dim": "col", "angle_vars": {"wind_from": "deg"}}}
    )
    assert hinted.hints == StructureHints(y_dim="row", x_dim="col", angle_vars={"wind_from": "deg"})
    with pytest.raises(ValidationError, match="y_dim and x_dim"):
        StructureHints(y_dim="row")
    with pytest.raises(ValidationError):
        StructureHints(angle_vars={"a": "furlongs"})  # type: ignore[dict-item]
    with pytest.raises(ValidationError, match="disagree"):
        DataExploreRequest(time_dim="week", hints=StructureHints(time_dim="day"))
