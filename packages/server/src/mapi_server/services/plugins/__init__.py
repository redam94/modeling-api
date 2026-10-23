"""Uploaded code (v0.19): plugins, versions, review, the mount gate and notifications.

The control plane never opens an uploaded wheel: it stores the bytes, hashes them, and
reads the size-capped JSON results of the sandboxed ``plugin_inspect`` and
``plugin_describe`` runs back as data.

* :mod:`.gate` — who may mount which version (``pins_problem``) and which versions a
  caller's catalog view shows (``visible_versions``);
* :mod:`.store` — the one state writer (``set_state``), claims, upload tickets,
  ``:complete`` and the REST views;
* :mod:`.review` — ask for approval, approve or reject (a second tenant admin), yank;
* :mod:`.uploaders` — who may upload (D1/D2), attested uploaders, tenant settings;
* :mod:`.usage` — what uses a version (before a yank);
* :mod:`.notify` — mail and webhooks for every plugin event;
* ``pipeline``, ``finalize``, ``sweeps`` (WP6) — the sandboxed runs and the housekeeping;
* ``view``, ``refs`` (WP7) — the tenant catalog view and overlay pins of admissions.

Submodules are imported by name (``from mapi_server.services.plugins import gate``); this
package re-exports nothing, so the pipeline and the view can import the state writer and
the gate without an import cycle.
"""

from __future__ import annotations
