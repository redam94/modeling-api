---
description: Save a working workflow as a team template so everybody in your project or tenant can start from it - no code needed.
---

# Team workflow templates <span class="mapi-badge">v0.18</span>

Got a workflow that works? Save it as a **team template**, and your teammates can start their
own copy from the workflow gallery. Use it for the checks your team runs every week, or for a
model bake-off that everybody should run the same way.

## Save one

1. Open the workflow in the editor and make sure it validates.
2. Open the menu and pick **Save as team template**.
3. Give it a title and a short About card: what it does and when to use it.
4. Pick the scope. Editors save to the project. Tenant admins can also save to the tenant, so
   every project sees it.

A team template must validate when you save it (a draft workflow may have issues; a template
may not). The platform validates it against the node contracts that it knows: the built-in
nodes and the nodes of installed plugins.

In **Make your own › Workflow templates** you can list, edit and archive the templates.

## The document

A team template is a `WorkflowTemplate` document: an About card plus a complete workflow:

```yaml
--8<-- "extend_workflow_template.yaml"
```

This is the check that runs when you save and again when somebody starts from it
(`mapi_spec.library.workflow_template_issues`):

```python
--8<-- "extend_library.py:workflow"
```

## Start from one

The workflow gallery has three sections:

| Section | What it lists |
|---|---|
| Built in | Templates made and tested by the platform team |
| From your team | Team templates of your project and your tenant |
| Installed | Templates from plugins that your operators installed ([plugin templates](develop/workflow-templates.md)) |

**Start from this** asks the server for the template's document
(`POST /v1/library/{item_id}:use`). The app then makes a new workflow from it
(`POST /v1/workflows` with the document). The new workflow carries the label
`mapi.library/template`, with the item and the revision it came from. It is your copy:
later revisions of the template do not change it.

!!! note "A template that uses a removed node"
    The platform validates the template again each time somebody starts from it. If a node
    that it uses is no longer in the catalog (for example, a plugin was removed), **Start
    from this** is refused with an issue that points at that node. Nothing is created.

## What next

- Need a step in your workflow that no built-in node does? Write a workflow node in Python:
  [Develop a workflow node](develop/workflow-nodes.md).
- More workflows to copy: the [workflow cookbook](../guides/workflow-cookbook.md).
