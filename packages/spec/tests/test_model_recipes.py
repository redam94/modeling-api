"""v0.18 model recipes (WP2): compile goldens, refusals with pointers, data shapes, presets."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, Issue, Severity, has_errors
from mapi_spec.library import (
    LABEL_MODEL,
    LABEL_PREP,
    ChartTemplateDoc,
    LibraryDoc,
    LibraryKind,
    LibraryRef,
    ModelRecipeDoc,
    parse_library_doc,
)
from mapi_spec.model_recipes import (
    DECLARATIVE_MODEL_ID,
    DECLARATIVE_PLOTS,
    CompiledModel,
    compile_model_recipe,
    declarative_settings,
    exposed_settings_schema,
)
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_spec

pytestmark = pytest.mark.unit

ITEM = "lib_01jaf3q2x8m9v7k5h4g3f2e1d0"
PREP_ITEM = "lib_01jaf3q2x8m9v7k5h4g3f2e1d1"
CHART_ITEM = "lib_01jaf3q2x8m9v7k5h4g3f2e1d2"

STORE_SALES: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "store-sales", "title": "Store sales with promo lift", "tags": ["retail"]},
    "base": "declarative",
    "declarative": {
        "outcome": {"var": "sales", "family": "negative_binomial"},
        "terms": [
            {
                "var": "price",
                "transform": [{"use": "log1p"}, {"use": "standardize", "with": {"over": ["obs"]}}],
                "prior": {"dist": "normal", "sigma": 0.5},
                "sign": "negative",
            },
            {
                "var": "promo",
                "prior": {"dist": "normal", "sigma": 1},
                "sign": "positive",
                "varies_by": "store",
            },
        ],
        "groups": [{"dim": "store"}],
    },
    "plots": ["core.ppc"],
}

N = {"dist": "normal", "mu": 0.0}
HN = {"dist": "half_normal", "mu": 0.0, "sigma": 1.0}

PANEL = DataSchema.model_validate(
    {
        "dims": {
            "week": {"kind": "time", "policy": "extend"},
            "store": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            v: {"dims": ["week", "store"], "dtype": "float64"}
            for v in ("sales", "price", "promo", "footfall")
        },
    }
)
LONG = DataSchema.model_validate(
    {
        "layout": "long",
        "dims": {"row": {"kind": "integer", "policy": "extend"}},
        "vars": {
            "sales": {"dims": ["row"], "dtype": "float64"},
            "price": {"dims": ["row"], "dtype": "float64"},
            "promo": {"dims": ["row"], "dtype": "float64"},
            "store": {"dims": ["row"], "dtype": "str"},
        },
    }
)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    """The checkout catalog, plus a stand-in ``tabular.declarative_glm`` until WP-X7 lands."""
    registry = pytest.importorskip("mapi_sdk.registry")
    live: Catalog = registry.default_registry().catalog()
    if live.resolve_model(f"{DECLARATIVE_MODEL_ID}@^1") is not None:
        return live
    glm = live.resolve_model("tabular.hierarchical_glm@^1")
    assert glm is not None
    manifest = glm.manifest.model_dump(mode="json", by_alias=True)
    manifest.update(id=DECLARATIVE_MODEL_ID, version="1.0.0", settings_schema={"type": "object"})
    data = live.model_dump(mode="json", by_alias=True)
    data["models"].append({"manifest": manifest})
    return Catalog.model_validate(data)


def _doc(raw: dict[str, Any]) -> ModelRecipeDoc:
    doc = parse_library_doc(copy.deepcopy(raw))
    assert isinstance(doc, ModelRecipeDoc)
    return doc


def _mutated(**changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(STORE_SALES)
    raw["declarative"].update(changes)
    return raw


def _errors(c: CompiledModel | list[Issue]) -> list[tuple[str, str]]:
    issues = c.issues if isinstance(c, CompiledModel) else c
    return [(i.pointer, i.message) for i in issues if i.severity is Severity.ERROR]


def _frag(c: CompiledModel) -> dict[str, Any]:
    return c.spec_fragment()


def _spec(c: CompiledModel) -> ModelSpec:
    frag = _frag(c)
    return ModelSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ModelSpec",
            "metadata": {"name": "compiled", "project": "growth", **frag.get("metadata", {})},
            "data": {"dataset": "dataset://growth/stores@latest", **frag.get("data", {})},
            "pipeline": frag["pipeline"],
            "model": frag["model"],
            "outputs": frag.get("outputs", {}),
        }
    )


# ----------------------------------------------------------------------------- goldens


def test_golden_store_sales_declarative(catalog: Catalog) -> None:
    c = compile_model_recipe(_doc(STORE_SALES), catalog, item_ref=f"{ITEM}-r1")
    assert c.ok
    assert [(i.code, i.severity) for i in c.issues] == [
        (ErrorCode.LIBRARY_DATA_SHAPE, Severity.INFO)
    ]
    assert _frag(c) == {
        "data": {"bind": {"vars": {"y": "sales"}}},
        "pipeline": [
            {"id": "t0_log1p", "use": "core.log1p@^1", "with": {"vars": ["price"]}},
            {
                "id": "t1_standardize",
                "use": "core.standardize@^1",
                "with": {"vars": ["price"], "over": ["obs"]},
            },
        ],
        "model": {
            "use": "tabular.declarative_glm@^1",
            "settings": {
                "family": "negative_binomial",
                "covariates": ["price", "promo"],
                "groups": ["store"],
                "terms": {
                    "price": {**N, "sigma": 0.5, "sign": "negative"},
                    "promo": {**N, "sigma": 1.0, "sign": "positive", "varies_by": "store"},
                },
                "interactions": [],
                "intercept": {**N, "sigma": 2.0},
                "group_sigma": {"store": HN},
                "noise": HN,
            },
        },
        "outputs": {"plots": ["core.ppc"]},
        "metadata": {"labels": {LABEL_MODEL: f"{ITEM}-r1"}},
    }


def test_golden_every_distribution_interactions_and_crossed_groups(catalog: Catalog) -> None:
    raw = {
        **STORE_SALES,
        "declarative": {
            "outcome": {"var": "rating", "family": "ordinal", "n_categories": 5},
            "intercept": {"dist": "student_t", "mu": 1.0, "sigma": 3.0, "nu": 4},
            "terms": [
                {"var": "a", "prior": {"dist": "student_t", "sigma": 2, "nu": 3}},
                {
                    "var": "b",
                    "prior": {"dist": "laplace", "mu": -1, "sigma": 0.2},
                    "sign": "negative",
                },
                {
                    "var": "c",
                    "prior": {"dist": "half_normal", "sigma": 0.3},
                    "sign": "positive",
                    "varies_by": "region",
                },
                {"var": "d", "prior": {"dist": "exponential", "sigma": 1.5}, "sign": "negative"},
                {
                    "var": "e",
                    "prior": {"dist": "lognormal", "mu": -2, "sigma": 0.5},
                    "sign": "positive",
                    "varies_by": "store",
                },
            ],
            "interactions": [{"a": "a", "b": "b"}, {"a": "c", "b": "e", "prior": {"sigma": 0.1}}],
            "groups": [
                {"dim": "store"},
                {"dim": "region", "sigma_prior": {"dist": "exponential", "sigma": 2}},
                {"dim": "month", "sigma_prior": {"dist": "lognormal", "mu": 0, "sigma": 1}},
            ],
            "noise": {"dist": "exponential", "sigma": 0.7},
        },
        "plots": [],
    }
    m = _doc(raw).declarative
    assert m is not None
    assert declarative_settings(m) == {
        "family": "ordinal",
        "n_categories": 5,
        "covariates": ["a", "b", "c", "d", "e"],
        "groups": ["store", "region", "month"],
        "terms": {
            "a": {"dist": "student_t", "mu": 0.0, "sigma": 2.0, "nu": 3.0, "sign": "any"},
            "b": {"dist": "laplace", "mu": -1.0, "sigma": 0.2, "sign": "negative"},
            "c": {
                "dist": "half_normal",
                "mu": 0.0,
                "sigma": 0.3,
                "sign": "positive",
                "varies_by": "region",
            },
            "d": {"dist": "exponential", "mu": 0.0, "sigma": 1.5, "sign": "negative"},
            "e": {
                "dist": "lognormal",
                "mu": -2.0,
                "sigma": 0.5,
                "sign": "positive",
                "varies_by": "store",
            },
        },
        "interactions": [
            {"a": "a", "b": "b", "prior": {**N, "sigma": 0.5}},
            {"a": "c", "b": "e", "prior": {**N, "sigma": 0.1}},
        ],
        "intercept": {"dist": "student_t", "mu": 1.0, "sigma": 3.0, "nu": 4.0},
        "group_sigma": {
            "store": HN,
            "region": {"dist": "exponential", "mu": 0.0, "sigma": 2.0},
            "month": {"dist": "lognormal", "mu": 0.0, "sigma": 1.0},
        },
        "noise": {"dist": "exponential", "mu": 0.0, "sigma": 0.7},
    }
    c = compile_model_recipe(_doc(raw), catalog)
    assert _errors(c) == []
    assert c.pipeline == []
    # a declarative recipe that lists no plots draws the defaults
    assert _frag(c)["outputs"] == {"plots": list(DECLARATIVE_PLOTS)}


def test_golden_identical_transforms_merge_in_term_order(catalog: Catalog) -> None:
    raw = _mutated(
        terms=[
            {
                "var": "tv",
                "transform": [
                    {"use": "adstock", "with": {"alpha": 0.5, "along": "obs"}},
                    {"use": "saturation", "with": {"kind": "hill"}},
                ],
                "prior": {"dist": "half_normal"},
                "sign": "positive",
            },
            {
                "var": "search",
                "transform": [
                    {"use": "adstock", "with": {"along": "obs", "alpha": 0.5}},
                    {"use": "saturation", "with": {"kind": "hill"}},
                ],
                "prior": {"dist": "half_normal"},
                "sign": "positive",
            },
            {"var": "price", "transform": [{"use": "standardize"}], "prior": {}},
            {
                "var": "temp",
                "transform": [{"use": "standardize"}, {"use": "max_abs_scale"}],
                "prior": {},
            },
            {
                "var": "radio",
                "transform": [{"use": "adstock", "with": {"alpha": 0.2, "along": "obs"}}],
                "prior": {"dist": "half_normal"},
                "sign": "positive",
            },
        ],
        groups=[],
    )
    raw["declarative"]["outcome"] = {"var": "sales"}
    c = compile_model_recipe(_doc(raw), catalog)
    assert _errors(c) == []
    assert _frag(c)["pipeline"] == [
        {
            "id": "t0_adstock",
            "use": "core.adstock@^1",
            "with": {"vars": ["tv", "search"], "alpha": 0.5, "along": "obs"},
        },
        {
            "id": "t1_standardize",
            "use": "core.standardize@^1",
            "with": {"vars": ["price", "temp"], "over": ["obs"]},
        },
        {
            "id": "t2_adstock",
            "use": "core.adstock@^1",
            "with": {"vars": ["radio"], "alpha": 0.2, "along": "obs"},
        },
        {
            "id": "t3_saturation",
            "use": "core.saturation@^1",
            "with": {"vars": ["tv", "search"], "kind": "hill"},
        },
        {
            "id": "t4_max_abs_scale",
            "use": "core.max_abs_scale@^1",
            "with": {"vars": ["temp"], "over": ["obs"]},
        },
    ]


def test_golden_preset_over_sarimax_with_exposed_keys(catalog: Catalog) -> None:
    raw = {
        "apiVersion": "mapi/v1",
        "kind": "ModelRecipe",
        "metadata": {"name": "weekly-sarimax", "title": "Weekly SARIMAX"},
        "base": "ts.sarimax@^1",
        "settings": {"seasonal_order": [1, 0, 0, 52], "order": [1, 1, 0]},
        "exposed": ["order", "exog"],
        "fit": {"mode": "train_only"},
        "plots": ["core.ppc_fan@^1"],
    }
    doc = _doc(raw)
    c = compile_model_recipe(
        doc, catalog, item_ref=f"{ITEM}-r4", exposed={"order": [2, 1, 1], "exog": ["price"]}
    )
    assert c.issues == []
    assert _frag(c) == {
        "pipeline": [],
        "model": {
            "use": "ts.sarimax@^1",
            "settings": {"seasonal_order": [1, 0, 0, 52], "order": [2, 1, 1], "exog": ["price"]},
            "fit": {"mode": "train_only"},
        },
        "outputs": {"plots": ["core.ppc_fan@^1"]},
        "metadata": {"labels": {LABEL_MODEL: f"{ITEM}-r4"}},
    }
    form = exposed_settings_schema(doc, catalog)
    assert form is not None
    assert form["additionalProperties"] is False
    props = form["properties"]
    assert isinstance(props, dict) and set(props) == {"order", "exog"}
    order = props["order"]
    assert isinstance(order, dict) and order["default"] == [1, 1, 0]


def test_golden_fallback_target(catalog: Catalog) -> None:
    raw = copy.deepcopy(STORE_SALES)
    for t in raw["declarative"]["terms"]:
        t["sign"] = "any"
    c = compile_model_recipe(_doc(raw), catalog, schema=PANEL, target="fallback")
    assert _errors(c) == []
    assert _frag(c)["model"] == {
        "use": "tabular.hierarchical_glm@^1.1",
        "settings": {
            "group_dim": "store",
            "family": "negative_binomial",
            "covariates": ["price", "promo"],
            "varying_slopes": ["promo"],
            "intercept_sigma": 2.0,
            "group_sigma_scale": 1.0,
            "beta_sigma": 1.0,
            "noise_sigma": 1.0,
        },
    }
    # the compiled spec validates against the checkout catalog and the panel's schema
    v = validate_spec(_spec(c), catalog, PANEL)
    assert not has_errors(v.issues), [(i.pointer, i.message) for i in v.issues]


def test_fallback_refuses_what_it_cannot_express(catalog: Catalog) -> None:
    raw = _mutated(interactions=[{"a": "price", "b": "promo"}], groups=[])
    raw["declarative"]["terms"][1].pop("varies_by")
    c = compile_model_recipe(_doc(raw), catalog, target="fallback")
    assert _errors(c) == [
        ("/declarative/groups", "The fallback model needs exactly one group."),
        ("/declarative/interactions/0", "The fallback model has no interactions."),
        ("/declarative/terms/0/sign", "The fallback model has no sign constraints."),
        ("/declarative/terms/1/sign", "The fallback model has no sign constraints."),
    ]


# -------------------------------------------------------------------------- data shapes


def test_dense_panel_gets_the_to_long_step(catalog: Catalog) -> None:
    c = compile_model_recipe(_doc(STORE_SALES), catalog, schema=PANEL)
    assert c.ok, _errors(c)
    frag = _frag(c)
    assert frag["data"] == {"bind": {"vars": {"y": "sales"}}}
    assert frag["pipeline"][0] == {
        "id": "shape_long",
        "use": "core.to_long@^1",
        "with": {
            "group_dim": "store",
            "within_dim": "week",
            "row_dim": "obs",
            "vars": ["y", "price", "promo", "footfall"],
        },
    }
    assert [s["id"] for s in frag["pipeline"]] == ["shape_long", "t0_log1p", "t1_standardize"]


def test_long_table_binds_its_row_dim(catalog: Catalog) -> None:
    c = compile_model_recipe(_doc(STORE_SALES), catalog, schema=LONG)
    assert c.ok, _errors(c)
    assert c.bind.model_dump() == {"dims": {"obs": "row"}, "vars": {"y": "sales"}}
    assert [s.id for s in c.pipeline] == ["t0_log1p", "t1_standardize"]
    # the fallback model needs the group as a dim with an index
    fb = compile_model_recipe(
        _doc(_mutated(terms=[{"var": "price", "prior": {}}])),
        catalog,
        schema=LONG,
        target="fallback",
    )
    assert [(i.pointer, i.code) for i in fb.issues if i.is_error] == [
        ("/declarative/groups/0/dim", ErrorCode.LIBRARY_DATA_SHAPE)
    ]


@pytest.mark.parametrize(
    ("schema", "pointer"),
    [
        (
            DataSchema.model_validate(
                {
                    "dims": {
                        "week": {"kind": "time", "policy": "extend"},
                        "store": {"kind": "categorical"},
                        "sku": {"kind": "categorical"},
                    },
                    "vars": {
                        v: {"dims": ["week", "store", "sku"], "dtype": "float64"}
                        for v in ("sales", "price", "promo")
                    },
                }
            ),
            "/declarative/outcome/var",
        ),
        (
            DataSchema.model_validate(
                {
                    "dims": {
                        "week": {"kind": "time", "policy": "extend"},
                        "store": {"kind": "categorical"},
                    },
                    "vars": {
                        "sales": {"dims": ["week", "store"], "dtype": "float64"},
                        "price": {"dims": ["week"], "dtype": "float64"},
                        "promo": {"dims": ["week", "store"], "dtype": "float64"},
                    },
                }
            ),
            "/declarative/terms/0/var",
        ),
    ],
)
def test_unshaped_data_is_refused_with_a_hint(
    catalog: Catalog, schema: DataSchema, pointer: str
) -> None:
    c = compile_model_recipe(_doc(STORE_SALES), catalog, schema=schema)
    shape = [i for i in c.issues if i.code is ErrorCode.LIBRARY_DATA_SHAPE and i.is_error]
    assert [i.pointer for i in shape] == [pointer]
    assert shape[0].suggested_action is not None and "long layout" in shape[0].suggested_action


def test_missing_variables_point_at_the_term(catalog: Catalog) -> None:
    raw = _mutated(terms=[{"var": "price", "prior": {}}, {"var": "discount", "prior": {}}])
    c = compile_model_recipe(_doc(raw), catalog, schema=PANEL)
    assert _errors(c) == [("/declarative/terms/1/var", "The dataset has no variable 'discount'.")]


# ----------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("change", "pointer", "message"),
    [
        (
            {"terms": [{"var": "price", "prior": {}, "varies_by": "region"}]},
            "/declarative/terms/0/varies_by",
            "varies_by 'region' is not one of the groups.",
        ),
        (
            {"terms": [{"var": "sales", "prior": {}}]},
            "/declarative/terms/0/var",
            "A term cannot use the outcome variable.",
        ),
        (
            {"terms": [{"var": "y", "prior": {}}]},
            "/declarative/terms/0/var",
            "The name 'y' is kept for the outcome. Rename the variable.",
        ),
        (
            {"terms": [{"var": "price", "prior": {}}, {"var": "price", "prior": {}}]},
            "/declarative/terms/1/var",
            "Variable 'price' is in more than one term.",
        ),
        (
            {"terms": [{"var": "price", "prior": {"nu": 3}}]},
            "/declarative/terms/0/prior/nu",
            "Only the student_t prior uses nu.",
        ),
        (
            {"outcome": {"var": "sales", "family": "ordinal"}},
            "/declarative/outcome/n_categories",
            "The ordinal family needs n_categories.",
        ),
        (
            {"outcome": {"var": "sales", "n_categories": 3}},
            "/declarative/outcome/n_categories",
            "Only the ordinal family uses n_categories.",
        ),
        (
            {"groups": [{"dim": "store", "sigma_prior": {"dist": "normal"}}]},
            "/declarative/groups/0/sigma_prior/dist",
            "A scale prior must be half_normal, exponential or lognormal.",
        ),
        (
            {"groups": [{"dim": "store"}, {"dim": "store"}]},
            "/declarative/groups/1/dim",
            "Group 'store' is listed twice.",
        ),
        (
            {"noise": {"dist": "laplace"}},
            "/declarative/noise/dist",
            "A scale prior must be half_normal, exponential or lognormal.",
        ),
        (
            {"interactions": [{"a": "price", "b": "weather"}]},
            "/declarative/interactions/0/b",
            "'weather' is not a term variable.",
        ),
        (
            {"interactions": [{"a": "price", "b": "price"}]},
            "/declarative/interactions/0/b",
            "An interaction needs two different terms.",
        ),
        (
            {"interactions": [{"a": "price", "b": "promo"}, {"a": "promo", "b": "price"}]},
            "/declarative/interactions/1",
            "This interaction is listed twice.",
        ),
        (
            {
                "terms": [
                    {
                        "var": "price",
                        "prior": {},
                        "transform": [{"use": "log1p", "with": {"vars": ["x"]}}],
                    }
                ]
            },
            "/declarative/terms/0/transform/0/with/vars",
            "The term sets vars. Remove it.",
        ),
        (
            {
                "terms": [
                    {
                        "var": "price",
                        "prior": {},
                        "transform": [{"use": "adstock", "with": {"kind": "boxcar"}}],
                    }
                ]
            },
            "/declarative/terms/0/transform/0/with/kind",
            "value is not one of the allowed options",
        ),
    ],
)
def test_refusals_point_into_the_recipe(
    catalog: Catalog, change: dict[str, Any], pointer: str, message: str
) -> None:
    c = compile_model_recipe(_doc(_mutated(**change)), catalog)
    assert (pointer, message) in _errors(c), _errors(c)
    assert all(i.pointer.startswith("/") for i in c.issues if i.is_error)


def test_soft_prior_advice_is_a_warning(catalog: Catalog) -> None:
    raw = _mutated(terms=[{"var": "price", "prior": {"dist": "half_normal", "mu": 2}}], groups=[])
    c = compile_model_recipe(_doc(raw), catalog)
    assert _errors(c) == []
    warnings = [(i.pointer, i.message) for i in c.issues if i.severity is Severity.WARNING]
    assert warnings == [
        ("/declarative/terms/0/prior/mu", "The half_normal prior does not use mu."),
        (
            "/declarative/terms/0/sign",
            "The half_normal prior allows only positive effects. "
            "Set the sign to positive or negative.",
        ),
    ]


def test_declarative_recipe_parts(catalog: Catalog) -> None:
    raw = copy.deepcopy(STORE_SALES)
    raw.pop("declarative")
    c = compile_model_recipe(_doc(raw), catalog)
    assert _errors(c)[0][0] == "/declarative"
    c = compile_model_recipe(
        _doc({**STORE_SALES, "settings": {"a": 1}, "exposed": ["a"]}), catalog, exposed={"a": 2}
    )
    assert {p for p, _ in _errors(c)} == {"/settings", "/exposed", "/exposed/a"}


def test_preset_refusals(catalog: Catalog) -> None:
    base = {
        "apiVersion": "mapi/v1",
        "kind": "ModelRecipe",
        "metadata": {"name": "p", "title": "P"},
        "base": "ts.sarimax@^1",
    }
    c = compile_model_recipe(
        _doc(
            {
                **base,
                "settings": {"ordr": [1, 0, 0], "maxiter": 1},
                "exposed": ["trend", "nope"],
                "declarative": STORE_SALES["declarative"],
            }
        ),
        catalog,
        exposed={"trend": "x", "method": "nm"},
    )
    assert _errors(c) == [
        ("/declarative", "Only a recipe with base: declarative uses declarative."),
        ("/exposed/method", "Setting 'method' is not exposed."),
        ("/settings/ordr", "'ordr' is not a setting of ts.sarimax."),
        ("/exposed/1", "'nope' is not a setting of ts.sarimax."),
        ("/settings/maxiter", "value is below the minimum"),
        ("/settings/trend", "value is not one of the allowed options"),
    ]
    gone = compile_model_recipe(_doc({**base, "base": "acme.store_mean@^1"}), catalog)
    assert [(i.pointer, i.code) for i in gone.issues] == [
        ("/base", ErrorCode.EXTENSION_UNAVAILABLE)
    ]
    assert exposed_settings_schema(_doc({**base, "base": "acme.store_mean@^1"}), catalog) is None


def test_unknown_plots_and_models_are_unavailable(catalog: Catalog) -> None:
    c = compile_model_recipe(
        _doc({**STORE_SALES, "plots": ["core.ppc", "acme.weekly@^1"]}), catalog
    )
    assert [(i.pointer, i.code) for i in c.issues if i.is_error] == [
        ("/plots/1", ErrorCode.EXTENSION_UNAVAILABLE)
    ]
    bare = Catalog.model_validate({"steps": [], "models": [], "plots": []})
    c = compile_model_recipe(_doc(STORE_SALES), bare)
    assert {(i.pointer, i.code) for i in c.issues if i.is_error} == {
        ("/base", ErrorCode.EXTENSION_UNAVAILABLE),
        ("/declarative/terms/0/transform/0/use", ErrorCode.EXTENSION_UNAVAILABLE),
        ("/declarative/terms/0/transform/1/use", ErrorCode.EXTENSION_UNAVAILABLE),
        ("/plots/0", ErrorCode.EXTENSION_UNAVAILABLE),
    }


# ------------------------------------------------------------------- prep + chart refs

PREP: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "PrepPreset",
    "metadata": {"name": "foot", "title": "Footfall"},
    "slots": {"v": {"kind": "var_list", "label": "Vars"}, "time": {"kind": "dim", "label": "T"}},
    "steps": [
        {
            "id": "fill",
            "use": "core.impute@^1",
            "with": {"vars": "$slot:v", "method": "ffill", "along": "$slot:time"},
        }
    ],
}
CHART: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ChartTemplate",
    "metadata": {"name": "dots", "title": "Dots"},
    "source_recipe": "core.fit_vs_actual@^1",
    "plot": {"data": {"name": "points"}, "mark": "point"},
}


def _resolver(ref: LibraryRef, kind: LibraryKind) -> LibraryDoc | None:
    if ref.item == PREP_ITEM and kind == "prep_preset":
        return parse_library_doc(PREP)
    if ref.item == CHART_ITEM and kind == "chart_template":
        doc = parse_library_doc(CHART)
        assert isinstance(doc, ChartTemplateDoc)
        return doc
    return None


def test_prep_preset_is_prepended_and_labelled(catalog: Catalog) -> None:
    raw = {
        **STORE_SALES,
        "prep": {"item": PREP_ITEM, "revision": 2},
        "charts": [{"item": CHART_ITEM}],
    }
    c = compile_model_recipe(
        _doc(raw),
        catalog,
        item_ref=f"{ITEM}-r1",
        resolve=_resolver,
        schema=PANEL,
        slots={"v": ["sales", "footfall"], "time": "week"},
    )
    assert c.ok, _errors(c)
    assert c.labels == {LABEL_MODEL: f"{ITEM}-r1", LABEL_PREP: f"{PREP_ITEM}-r2"}
    # the listed plot stays; the chart template's source recipe is drawn too
    assert _frag(c)["outputs"] == {"plots": ["core.ppc", "core.fit_vs_actual@^1"]}
    first = _frag(c)["pipeline"][0]
    # slot values name dataset variables; after the binding the outcome is "y"
    assert first == {
        "id": "p_fill",
        "use": "core.impute@^1",
        "with": {"vars": ["y", "footfall"], "method": "ffill", "along": "week"},
    }
    assert [s.id for s in c.pipeline] == ["p_fill", "shape_long", "t0_log1p", "t1_standardize"]


def test_prep_and_chart_refs_must_resolve(catalog: Catalog) -> None:
    other = "lib_01jaf3q2x8m9v7k5h4g3f2e1zz"
    raw = {**STORE_SALES, "prep": {"item": other}, "charts": [{"item": PREP_ITEM}]}
    c = compile_model_recipe(_doc(raw), catalog, resolve=_resolver)
    assert _errors(c) == [
        ("/prep/item", "The prep preset is not in the library."),
        ("/charts/0/item", "The chart template is not in the library."),
    ]
    assert c.labels == {LABEL_PREP: other}
    later = compile_model_recipe(_doc({**STORE_SALES, "prep": {"item": PREP_ITEM}}), catalog)
    assert [(i.pointer, i.severity) for i in later.issues if i.pointer == "/prep"] == [
        ("/prep", Severity.INFO)
    ]


def test_prep_issues_point_under_prep(catalog: Catalog) -> None:
    raw = {**STORE_SALES, "prep": {"item": PREP_ITEM}}
    c = compile_model_recipe(
        _doc(raw), catalog, resolve=_resolver, schema=PANEL, slots={"v": ["nope"]}
    )
    assert ("/prep/slots/v", "The dataset has no variable 'nope'.") in _errors(c)
    assert ("/prep/slots/time", "Slot 'time' (T) needs a value.") in _errors(c)


def test_declarative_default_plots_and_chart_sources(catalog: Catalog) -> None:
    """A declarative recipe with no plots draws predicted vs actual, the forest and the
    sampler check (v0.18 live: its fits had an empty plots.json and "Your charts" had nothing
    to restyle); listed plots are kept as they are, and a chart's source is added once."""
    bare = {k: v for k, v in STORE_SALES.items() if k != "plots"}
    c = compile_model_recipe(_doc(bare), catalog, schema=PANEL)
    assert c.ok, _errors(c)
    assert [str(p) for p in c.outputs.plots] == list(DECLARATIVE_PLOTS)
    v = validate_spec(_spec(c), catalog, PANEL)
    assert not has_errors(v.issues), [(i.pointer, i.message) for i in v.issues]
    # a chart over a default plot does not add it twice
    charted = compile_model_recipe(
        _doc({**bare, "charts": [{"item": CHART_ITEM}]}), catalog, resolve=_resolver
    )
    assert [str(p) for p in charted.outputs.plots] == list(DECLARATIVE_PLOTS)
    # the fallback target gets the same defaults
    fb = copy.deepcopy(bare)
    for t in fb["declarative"]["terms"]:
        t["sign"] = "any"
    fallback = compile_model_recipe(_doc(fb), catalog, schema=PANEL, target="fallback")
    assert [str(p) for p in fallback.outputs.plots] == list(DECLARATIVE_PLOTS)
    # a preset with no plots keeps the model's own (none listed)
    preset = compile_model_recipe(
        _doc(
            {
                "apiVersion": "mapi/v1",
                "kind": "ModelRecipe",
                "metadata": {"name": "p", "title": "P"},
                "base": "ts.sarimax@^1",
            }
        ),
        catalog,
    )
    assert preset.outputs.plots == []
