"""Plot recipes and the JSON-only PlotSpec sanitizer.

* :mod:`mapi_plots.sanitize` - the sanitizer (core install; JSON only, safe for the control
  plane).
* :mod:`mapi_plots.templates` - role/dim-bound plot templates (core install; JSON only).
* :mod:`mapi_plots.convert` - ``from_altair`` / ``from_plotly`` and Arrow dataset helpers
  (extra ``recipes``).
* :mod:`mapi_plots.recipes` and :mod:`mapi_plots.packs` - the standard recipe catalog and
  domain packs (extra ``recipes``), registered under the ``mapi.plots`` entry-point group.

Names are resolved lazily so ``import mapi_plots`` never imports numpy, xarray, altair or
plotly.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.15.1"

_LAZY: dict[str, str] = {
    # sanitizer (core)
    "SanitizeError": "mapi_plots.sanitize",
    "SanitizeLimits": "mapi_plots.sanitize",
    "SanitizeResult": "mapi_plots.sanitize",
    "SanitizeRule": "mapi_plots.sanitize",
    "PLOTLY_REF_SCHEMA": "mapi_plots.sanitize",
    "check_plot": "mapi_plots.sanitize",
    "sanitize_plot": "mapi_plots.sanitize",
    "sanitize_spec": "mapi_plots.sanitize",
    "strip_html": "mapi_plots.sanitize",
    # templates (core)
    "BoundField": "mapi_plots.templates",
    "TemplateError": "mapi_plots.templates",
    "apply_template": "mapi_plots.templates",
    "resolve_bindings": "mapi_plots.templates",
    # conversion (extra: recipes)
    "from_altair": "mapi_plots.convert",
    "from_plotly": "mapi_plots.convert",
    "frame_to_arrow": "mapi_plots.convert",
    "arrow_to_frame": "mapi_plots.convert",
    "decode_typed_array": "mapi_plots.convert",
    # recipes (extra: recipes)
    "Recipe": "mapi_plots.recipes",
    "recipe": "mapi_plots.recipes",
    "standard_recipes": "mapi_plots.recipes",
    "get_recipe": "mapi_plots.recipes",
    "render_template": "mapi_plots.recipes",
}

__all__ = [
    "PLOTLY_REF_SCHEMA",
    "BoundField",
    "Recipe",
    "SanitizeError",
    "SanitizeLimits",
    "SanitizeResult",
    "SanitizeRule",
    "TemplateError",
    "__version__",
    "apply_template",
    "arrow_to_frame",
    "check_plot",
    "decode_typed_array",
    "frame_to_arrow",
    "from_altair",
    "from_plotly",
    "get_recipe",
    "recipe",
    "render_template",
    "resolve_bindings",
    "sanitize_plot",
    "sanitize_spec",
    "standard_recipes",
    "strip_html",
]

if TYPE_CHECKING:  # pragma: no cover
    from mapi_plots.convert import (
        arrow_to_frame,
        decode_typed_array,
        frame_to_arrow,
        from_altair,
        from_plotly,
    )
    from mapi_plots.recipes import Recipe, get_recipe, recipe, render_template, standard_recipes
    from mapi_plots.sanitize import (
        PLOTLY_REF_SCHEMA,
        SanitizeError,
        SanitizeLimits,
        SanitizeResult,
        SanitizeRule,
        check_plot,
        sanitize_plot,
        sanitize_spec,
        strip_html,
    )
    from mapi_plots.templates import BoundField, TemplateError, apply_template, resolve_bindings


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'mapi_plots' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
