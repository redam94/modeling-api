"""Example documents of the scientific Data Explorer views (the ``examples`` of their JSON
Schema; the same documents are in ``packages/spec/tests/data/explorer_science/``)."""

from __future__ import annotations

from typing import Any, Final

__all__ = ["SCIENCE_VIEW_EXAMPLES"]

STRUCTURE: Final[dict[str, Any]] = {
    "caption_plain": "Shows the shapes the explorer found in the data and the part each dimension "
    "plays.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.structure/v1",
    "shapes": [
        {
            "kind": "image",
            "vars": ["quakes"],
            "dims": {"y": "row", "x": "col", "frame": "quarter"},
            "score": 0.9,
            "label_plain": "image: 9 x 12 cells, 44 quarter",
            "sections": ["images"],
        },
        {
            "kind": "timeseries",
            "vars": ["quakes"],
            "dims": {"time": "quarter"},
            "score": 0.6,
            "label_plain": "time series: 44 quarter (row and col summed)",
            "sections": ["timeseries"],
        },
    ],
    "dims": [
        {
            "name": "quarter",
            "size": 44,
            "kind": "time",
            "role_in_data": "time",
            "start": "2009-01-01",
            "end": "2019-10-01",
            "evenly_spaced": True,
        },
        {
            "name": "row",
            "size": 9,
            "kind": "integer",
            "role_in_data": "y",
            "start": 0,
            "end": 8,
            "evenly_spaced": True,
        },
        {
            "name": "col",
            "size": 12,
            "kind": "integer",
            "role_in_data": "x",
            "start": 0,
            "end": 11,
            "evenly_spaced": True,
        },
    ],
    "vars": [
        {
            "name": "quakes",
            "dims": ["quarter", "row", "col"],
            "shape": [44, 9, 12],
            "dtype": "int64",
            "role": "target",
            "units": "events",
            "used_as": "data",
        }
    ],
    "recommended": [
        "overview",
        "structure",
        "images",
        "quality",
        "distributions",
        "timeseries",
        "missingness",
        "outliers",
    ],
    "not_applicable": {
        "map": "the data has no polygons (ring_x and ring_y)",
        "network": "no variable links a dimension to itself (X, X_)",
        "tree": "no variable gives the parent of each node",
        "circular": "no variable is an angle or a time of day",
        "points": "no two variables give x and y positions",
        "profiles": "no variable lies along a non-time axis",
        "groups": "the data has no dimension with groups to compare",
        "correlation": "a correlation needs at least two numeric variables",
        "vif": "collinearity needs at least two drivers",
    },
    "time_dim": "quarter",
    "categorical_dims": [],
}

IMAGES: Final[dict[str, Any]] = {
    "caption_plain": "Shows each variable on its grid. Use the slider to go through the frames.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.images/v1",
    "fields": [
        {
            "output": "quakes",
            "units": "events",
            "facet": {},
            "kind": "scalar",
            "y_dim": "row",
            "x_dim": "col",
            "frame_dim": "quarter",
            "height": 2,
            "width": 3,
            "source_height": 2,
            "source_width": 3,
            "downsampled": False,
            "y_coords": [0, 1],
            "x_coords": [0, 1, 2],
            "colormap": "viridis",
            "log": False,
            "origin": "upper",
            "encoding": "linear",
            "n_draws": 0,
            "frames": [
                {
                    "label": "2009-01-01",
                    "mean": {"lo": 0.0, "hi": 7.0, "data": "AACSJCRJtm0AANq2"},
                    "draws": [],
                },
                {
                    "label": "2009-04-01",
                    "mean": {"lo": 0.0, "hi": 7.0, "data": "kiSSJEiSJEn///7/"},
                    "draws": [],
                },
            ],
            "omitted_frames": 0,
            "var": "quakes",
            "role": "target",
            "mask_var": "visible",
            "mask": {"lo": 0.0, "hi": 1.0, "data": "/v/+//7//v8AAP7/"},
            "frame_stats": {
                "coords": ["2009-01-01", "2009-04-01"],
                "mean": [1.833, 3.0],
                "sd": [1.94, 2.55],
                "min": [0.0, 1.0],
                "max": [5.0, 7.0],
                "missing_share": [0.0, 0.1667],
                "downsampled": False,
            },
            "histogram": {"edges": [0.0, 2.0, 4.0, 6.0, 8.0], "counts": [5, 2, 2, 2]},
            "log_counts": False,
            "stats": {
                "min": 0.0,
                "max": 7.0,
                "mean": 2.36,
                "sd": 2.29,
                "missing_share": 0.083,
                "saturated_share": 0.083,
            },
            "group_by": "label",
            "groups": [
                {"label": 0, "n": 2, "mean": {"lo": 0.0, "hi": 6.0, "data": "VRWqKv9/qmoAAP7/"}}
            ],
            "components": [],
        },
        {
            "output": "displacement",
            "units": "um",
            "facet": {},
            "kind": "vector",
            "y_dim": "row",
            "x_dim": "col",
            "component_dim": "component",
            "height": 2,
            "width": 2,
            "source_height": 4,
            "source_width": 4,
            "downsampled": True,
            "y_coords": [1, 3],
            "x_coords": [1, 3],
            "colormap": "magma",
            "log": False,
            "origin": "upper",
            "encoding": "linear",
            "n_draws": 0,
            "frames": [
                {
                    "mean": {"lo": 0.1, "hi": 1.0, "data": "AADGcXIc/v8="},
                    "draws": [],
                    "arrows": {"x": [0.5], "y": [0.5], "u": [[0.3]], "v": [[-0.2]]},
                }
            ],
            "omitted_frames": 0,
            "var": "displacement",
            "role": "target",
            "log_counts": False,
            "stats": {
                "min": 0.0,
                "max": 1.2,
                "mean": 0.45,
                "sd": 0.3,
                "missing_share": 0.0,
                "saturated_share": 0.0,
            },
            "groups": [],
            "components": [
                {"label": "x", "histogram": {"edges": [-1.0, 0.0, 1.0], "counts": [7, 9]}},
                {"label": "y", "histogram": {"edges": [-1.0, 0.0, 1.0], "counts": [8, 8]}},
            ],
        },
    ],
    "omitted": [],
}

PROFILES: Final[dict[str, Any]] = {
    "caption_plain": "Shows each variable along its axis, one curve per frame.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.profiles/v1",
    "items": [
        {
            "var": "cells",
            "units": "cells",
            "along": "position",
            "coords": [25.0, 75.0, 125.0, 175.0, 225.0],
            "frame_dim": "hours",
            "frames": [
                {"label": 0, "values": [10.0, 8.0, 3.0, 0.0, 0.0]},
                {"label": 12, "values": [12.0, 10.0, 6.0, 2.0, 0.0]},
            ],
            "omitted_frames": 0,
            "facet": {},
            "log_x": False,
            "log_y": False,
            "peak": [
                {"frame": 0, "at": 25.0, "value": 10.0},
                {"frame": 12, "at": 25.0, "value": 12.0},
            ],
            "downsampled": False,
        },
        {
            "var": "reference_dl",
            "units": "uK^2",
            "along": "multipole",
            "coords": [2, 3, 4, 5, 6],
            "frames": [{"values": [1000.0, 1050.0, 990.0, 1100.0, 1200.0]}],
            "omitted_frames": 0,
            "facet": {},
            "log_x": True,
            "log_y": False,
            "peak": [{"at": 6, "value": 1200.0}],
            "downsampled": False,
        },
    ],
    "omitted": [],
}

MAP: Final[dict[str, Any]] = {
    "caption_plain": "Shows each variable by area. Lines join neighbouring areas.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.map/v1",
    "area_dim": "district",
    "areas": ["A", "B", "C"],
    "rings": [
        {"area": 0, "x": [0.0, 1.0, 1.0, 0.0, 0.0], "y": [0.0, 0.0, 1.0, 1.0, 0.0]},
        {"area": 1, "x": [1.0, 2.0, 2.0, 1.0, 1.0], "y": [0.0, 0.0, 1.0, 1.0, 0.0]},
        {"area": 2, "x": [0.0, 2.0, 2.0, 0.0, 0.0], "y": [1.0, 1.0, 2.0, 2.0, 1.0]},
    ],
    "decimated": False,
    "bbox": [0.0, 0.0, 2.0, 2.0],
    "centroids": {"x": [0.5, 1.5, 1.0], "y": [0.5, 0.5, 1.5]},
    "layers": [
        {
            "var": "cases",
            "units": "cases",
            "role": "target",
            "values": [9.0, 3.0, 5.0],
            "frames": [],
            "derived_from": [],
        },
        {
            "var": "expected",
            "units": "cases",
            "role": "exposure",
            "values": [4.5, 3.0, 5.0],
            "frames": [],
            "derived_from": [],
        },
        {
            "var": "cases / expected",
            "values": [2.0, 1.0, 1.0],
            "frames": [],
            "derived_from": ["cases", "expected"],
        },
    ],
    "edges": [[0, 1], [0, 2], [1, 2]],
    "isolates": [],
    "moran": [
        {
            "var": "cases",
            "morans_i": -0.5,
            "expected": -0.5,
            "z": 0.0,
            "p_value": 1.0,
            "weights": "adjacency",
            "permutations": 999,
        }
    ],
}

NETWORK: Final[dict[str, Any]] = {
    "caption_plain": "Shows which nodes are linked: as a matrix and as a drawing.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.network/v1",
    "items": [
        {
            "var": "cowork",
            "node_dim": "lawyer",
            "facet": {},
            "binary": True,
            "signed": False,
            "directed": False,
            "weighted": False,
            "n_nodes": 4,
            "labels": [1, 2, 3, 4],
            "omitted_nodes": 0,
            "matrix": [
                [0.0, 1.0, 1.0, 0.0],
                [1.0, 0.0, 1.0, 0.0],
                [1.0, 1.0, 0.0, 1.0],
                [0.0, 0.0, 1.0, 0.0],
            ],
            "order": [0, 1, 2, 3],
            "positions": {"x": [0.0, 1.0, 0.5, 0.5], "y": [0.0, 0.0, 0.8, 1.6]},
            "edges": [
                {"i": 0, "j": 1, "w": 1.0},
                {"i": 0, "j": 2, "w": 1.0},
                {"i": 1, "j": 2, "w": 1.0},
                {"i": 2, "j": 3, "w": 1.0},
            ],
            "omitted_edges": 0,
            "degree": [2.0, 2.0, 3.0, 1.0],
            "degree_hist": {"edges": [1.0, 2.0, 3.0, 4.0], "counts": [1, 2, 1]},
            "density": 0.6667,
            "transitivity": 0.6,
            "components": {"count": 1, "largest_share": 1.0},
            "node_attrs": {"office": [1, 1, 2, 2]},
            "homophily": [{"attr": "office", "within_share": 0.25, "expected_share": 0.4}],
        }
    ],
    "omitted": [],
}

TREE: Final[dict[str, Any]] = {
    "caption_plain": "Shows the tree with the traits of its tips.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.tree/v1",
    "node_dim": "node",
    "n_nodes": 5,
    "n_tips": 3,
    "roots": [4],
    "depth": 2.0,
    "ultrametric": True,
    "layout": {
        "x": [2.0, 2.0, 2.0, 1.0, 0.0],
        "y": [0.0, 1.0, 2.0, 0.5, 1.25],
        "parent": [3, 3, 4, 4, -1],
        "is_tip": [True, True, True, False, False],
        "label": ["Homo_sapiens", "Pan_troglodytes", "Gorilla_gorilla", None, None],
    },
    "tips": [0, 1, 2],
    "tip_dim": "species",
    "matched_by": "label",
    "unmatched": 0,
    "traits": [
        {"var": "brain", "units": "cc", "values": [1350.0, 390.0, 500.0]},
        {"var": "body", "units": "g", "values": [62000.0, 45000.0, None]},
    ],
    "signal": [{"var": "brain", "morans_i": 0.12, "p_value": 0.4}],
}

CIRCULAR: Final[dict[str, Any]] = {
    "caption_plain": "Shows each angle or time of day as a rose. The line shows the mean "
    "direction.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.circular/v1",
    "items": [
        {
            "var": "wind_from",
            "units": "deg",
            "period": 360.0,
            "zero": "north",
            "clockwise": True,
            "bins": [0.0, 90.0, 180.0, 270.0, 360.0],
            "groups": [
                {
                    "label": "all",
                    "n": 100,
                    "counts": [10, 20, 50, 20],
                    "mean_direction": 182.0,
                    "resultant_length": 0.42,
                    "circular_sd": 75.0,
                    "rayleigh_z": 17.6,
                    "rayleigh_p": 0.0001,
                }
            ],
            "omitted_groups": 0,
        },
        {
            "var": "clock_time",
            "units": "h",
            "period": 24.0,
            "zero": "north",
            "clockwise": True,
            "group_by": "species",
            "bins": [0.0, 6.0, 12.0, 18.0, 24.0],
            "groups": [
                {
                    "label": "ocelot",
                    "n": 40,
                    "counts": [20, 2, 3, 15],
                    "mean_direction": 0.5,
                    "resultant_length": 0.6,
                    "circular_sd": 3.9,
                    "rayleigh_z": 14.4,
                    "rayleigh_p": 0.0001,
                },
                {
                    "label": "agouti",
                    "n": 30,
                    "counts": [1, 14, 14, 1],
                    "mean_direction": 12.0,
                    "resultant_length": 0.7,
                    "circular_sd": 3.2,
                    "rayleigh_z": 14.7,
                    "rayleigh_p": 0.0001,
                },
            ],
            "omitted_groups": 0,
        },
    ],
    "pairs": [
        {
            "a": "hour_lst",
            "b": "wind_from",
            "a_bins": [0.0, 12.0, 24.0],
            "b_bins": [0.0, 180.0, 360.0],
            "counts": [[30, 20], [10, 40]],
            "rho": 0.21,
        }
    ],
}

POINTS: Final[dict[str, Any]] = {
    "caption_plain": "Shows each point at its place. Select the colour.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.points/v1",
    "point_dim": "chip",
    "x": "wcs_col",
    "y": "wcs_row",
    "lattice": True,
    "n": 3,
    "sample": {"x": [1.0, 2.0, 3.0], "y": [0.0, 0.0, 1.0], "index": [0, 1, 2], "labels": [1, 2, 3]},
    "colour": [{"var": "cie_l", "values": [96.0, 51.6, 61.7]}],
    "srgb": ["#f5f5f5", "#a83a3a", "#3a7ca8"],
    "lab_vars": ["cie_l", "cie_a", "cie_b"],
    "answers": {
        "var": "response",
        "by_dim": "speaker",
        "terms": ["B", "F", "W"],
        "mode": ["W", "F", "B"],
        "agreement": [0.9, 0.6, 0.5],
        "counts": [[0, 1, 9], [2, 6, 2], [5, 3, 2]],
    },
}

_CIRCLE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAAAAACMmsGiAAAAE0lEQVR4nGNg"
    "+P+fAYiBJJgAcQF3nAv1WOTbDAAAAABJRU5ErkJggg=="
)
_SQUARE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAAAAACMmsGiAAAAEUlEQVR4nGNg"
    "AIP//2EEEAAAJ+wD/QDjYnMAAAAASUVORK5CYII="
)

GALLERY: Final[dict[str, Any]] = {
    "caption_plain": "Shows 4 of the 4 images. Use the chips to show one label or split.",
    "warnings_plain": [],
    "format": "mapi.data-explorer.gallery/v1",
    "var": "pixels",
    "frame_dim": "image",
    "y_dim": "y",
    "x_dim": "x",
    "channel_dim": "channel",
    "color": "gray",
    "source_height": 4,
    "source_width": 4,
    "thumb_height": 4,
    "thumb_width": 4,
    "n_images": 4,
    "items": [
        {"index": 0, "label": "0", "meta": {"label": "circle", "split": "train"},
         "png": _CIRCLE_PNG},
        {"index": 1, "label": "1", "meta": {"label": "square", "split": "train"},
         "png": _SQUARE_PNG},
        {"index": 2, "label": "2", "meta": {"label": "circle", "split": "test"},
         "png": _CIRCLE_PNG},
        {"index": 3, "label": "3", "meta": {"label": "square", "split": "test"},
         "png": _SQUARE_PNG},
    ],
    "facets": [
        {"name": "label", "source": "var", "counts": {"circle": 2, "square": 2}},
        {"name": "split", "source": "coord", "counts": {"test": 2, "train": 2}},
    ],
    "selection": "all",
    "other_vars": [],
}  # fmt: skip

SCIENCE_VIEW_EXAMPLES: Final[dict[str, dict[str, Any]]] = {
    "structure": STRUCTURE,
    "images": IMAGES,
    "gallery": GALLERY,
    "profiles": PROFILES,
    "map": MAP,
    "network": NETWORK,
    "tree": TREE,
    "circular": CIRCULAR,
    "points": POINTS,
}
