"""Uploaded namespaces and ids (docs/extend/upload.md, docs/extend/review-uploads.md).

Pure ``mapi_spec`` code: the same functions the server, the runner and the CLI use to tell an
uploaded id from a built-in or installed one. Nothing here needs a server; ``make docs-test``
runs the file top to bottom.
"""

from __future__ import annotations

# --8<-- [start:namespace]
from mapi_spec.plugins import (
    parse_upload_namespace,
    upload_dist_name,
    upload_namespace,
)

# The platform gives your workspace a 6-character tenant code on its first claim.
# You choose the short name ("acme") when you claim the plugin.
ns = upload_namespace("k3vq7m", "acme")
print(ns)  # t_k3vq7m_acme
print(parse_upload_namespace(ns))  # ('k3vq7m', 'acme')
print(upload_dist_name(ns))  # t-k3vq7m-acme: the distribution name of the wheel
# --8<-- [end:namespace]

assert ns == "t_k3vq7m_acme"
assert parse_upload_namespace(ns) == ("k3vq7m", "acme")
assert upload_dist_name(ns) == "t-k3vq7m-acme"


# --8<-- [start:ids]
from mapi_spec.plugins import is_uploaded_id

for id_ in (
    "t_k3vq7m_acme.store_mean",  # a model, step or plot recipe
    "transform.t_k3vq7m_acme_zero_share",  # a workflow node
    "t_k3vq7m_acme_weekly_check",  # a workflow template
    "core.adstock",  # built in
    "acme.group_mean",  # a plugin that an operator installed
):
    print(f"{id_:40} uploaded={is_uploaded_id(id_)}")
# --8<-- [end:ids]

assert is_uploaded_id("t_k3vq7m_acme.store_mean")
assert is_uploaded_id("transform.t_k3vq7m_acme_zero_share")
assert is_uploaded_id("t_k3vq7m_acme_weekly_check")
assert not is_uploaded_id("core.adstock")
assert not is_uploaded_id("acme.group_mean")


# --8<-- [start:states]
from mapi_spec.plugins import MOUNTABLE_STATES, PLUGIN_TRANSITIONS

for state, after in PLUGIN_TRANSITIONS.items():
    print(f"{state:18} -> {', '.join(sorted(after)) or '(final)'}")
print("can run:", sorted(MOUNTABLE_STATES))
# --8<-- [end:states]

assert MOUNTABLE_STATES == {"kit_passed", "awaiting_approval", "approved"}
assert "approved" in PLUGIN_TRANSITIONS["awaiting_approval"]
assert "approved" not in PLUGIN_TRANSITIONS["kit_passed"]  # always through a reviewer
