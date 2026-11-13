"""acme: extensions for modeling-api, scaffolded by ``mapi plugin init``.

* ``acme.model``: a model
  (``MODELS``, entry point ``mapi.models``)
* ``acme.steps``: a preprocessing step
  (``STEPS``, entry point ``mapi.steps``)
* ``acme.plots``: a plot recipe
  (``RECIPES``, entry point ``mapi.plots``)
* ``acme.nodes``: a workflow node
  (``NODES``, entry point ``mapi.workflow_nodes``)
* ``acme.templates``: a workflow template
  (``TEMPLATES``, entry point ``mapi.workflow_templates``)

Each module is registered through its own entry point (see ``pyproject.toml``). Ids live in
your namespace, ``acme``; the built-in namespaces (``core``, ``mmm``, ``ts``, ...)
and the platform ones (``mapi``, ``lib``, ``team``, ``t_*``, ...) are reserved.
"""
