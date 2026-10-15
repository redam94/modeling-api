# `mapi_plots`

The JSON-only sanitizer and templates (core install) and the recipes and converters (extra
`recipes`).

## Sanitizer

::: mapi_plots.sanitize
    options:
      members: [sanitize_plot, check_plot, sanitize_spec, SanitizeResult, SanitizeError, SanitizeLimits, SanitizeRule]
      heading_level: 3

## Templates

::: mapi_plots.templates
    options:
      members: [apply_template, resolve_bindings, template_issues, BoundField, TemplateError]
      heading_level: 3

## Converters

::: mapi_plots.convert
    options:
      members: [from_altair, from_plotly]
      heading_level: 3

## Recipes

::: mapi_plots.recipes
    options:
      members: [get_recipe, standard_recipes, render_plots, recipe, Recipe, RecipeSettings, Figure, render_template, builtin_templates]
      heading_level: 3
