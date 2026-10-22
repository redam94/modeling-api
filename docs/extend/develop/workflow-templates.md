---
description: Ship a workflow template in a modeling-api plugin - a TemplateCatalogEntry exported through mapi.workflow_templates and checked by check_template.
---

# Develop a workflow template

A plugin workflow template is a workflow that people start from, shipped with your plugin.
It is plain JSON: an About card for the gallery plus a workflow document. Use it to ship a
recommended way to use your nodes and models. (For a template your team saves from the
editor, with no code, see [Team workflow templates](../workflow-templates.md).)

```bash
mapi plugin init acme --kind template
```

## The scaffold

```python
--8<-- "extend_pkg/src/acme/templates.py"
```

- The id is `<namespace>_<name>` (`acme_weekly_check`). It must not be a built-in template
  id.
- The document may use built-in nodes and installed plugin nodes. The control plane
  validates it against every node contract it knows when it loads the catalog. A template
  that does not validate is hidden and listed as `template_invalid`.

## The entry point

```toml
[project.entry-points."mapi.workflow_templates"]
acme = "acme.templates:TEMPLATES"
```

## The contract kit

```python
--8<-- "extend_pkg/tests/test_template.py"
```

`check_template` checks the id, the About card and the document. Pass the node contracts the
document uses: `all_node_contracts([...])` merges your package's contracts with the built-in
ones.

## Use it

Once installed, the template is in the workflow gallery under **Installed**. Start from it
there, or with the CLI:

```bash
mapi workflows create --template acme_weekly_check
```
