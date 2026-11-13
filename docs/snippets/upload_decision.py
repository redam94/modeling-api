"""Bodies of the review calls for uploaded code (docs/extend/review-uploads.md).

A decision repeats the digests that the reviewer saw, so a version that changed in the
meantime is not approved by mistake. Pure ``mapi_spec`` code; no server is needed and
``make docs-test`` runs the file top to bottom.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

# What GET /v1/plugins/{plugin_id}/versions/{version_id} returned (the fields used here).
version = {
    "id": "plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
    "state": "awaiting_approval",
    "overlay_sha256": "sha256:" + "ab" * 32,
    "describe_digest": "sha256:" + "cd" * 32,
    "base_image_digest": "sha256:" + "ef" * 32,
}

# --8<-- [start:decision]
from mapi_spec.plugins import PluginDecision

approve = PluginDecision(
    overlay_sha256=version["overlay_sha256"],  # the file you looked at
    describe_digest=version["describe_digest"],  # the exports you looked at
    base_image_digest=version["base_image_digest"],  # the image the tests ran on
)
print(approve.model_dump_json(exclude_none=True))
# POST /v1/plugins/{plugin_id}/versions/{version_id}:approve with this body

reject = PluginDecision(
    **approve.model_dump(exclude={"note"}), note="The recipe links to a host we do not know."
)
print(reject.model_dump_json(exclude_none=True))
# POST ...:reject (a note is necessary)
# --8<-- [end:decision]

body = json.loads(approve.model_dump_json(exclude_none=True))
assert body == {k: version[k] for k in ("overlay_sha256", "describe_digest", "base_image_digest")}
assert json.loads(reject.model_dump_json())["note"].startswith("The recipe")

# a digest that is not a sha256 reference is refused before anything is sent
try:
    PluginDecision(overlay_sha256="abc", describe_digest="x", base_image_digest="y")
except ValidationError as exc:
    print(f"refused: {exc.error_count()} fields")
else:  # pragma: no cover - the DTO must refuse it
    raise AssertionError("a bad digest was accepted")


# --8<-- [start:yank]
from mapi_spec.plugins import PluginYank

yank = PluginYank(reason="security", note="Sends data to an unknown host.")
print(yank.model_dump_json(exclude_none=True))
# POST ...:yank (a web sign-in; API tokens cannot withdraw uploaded code)
# --8<-- [end:yank]

assert yank.reason == "security"


# --8<-- [start:lineage]
from mapi_spec.plugins import lineage_has_uploaded_code, lineage_pins

# the lineage of a model version that was fitted with an uploaded step
lineage = {
    "overlays": [
        {
            "plugin_version_id": version["id"],
            "namespace": "t_k3vq7m_acme",
            "sha256": version["overlay_sha256"],
        }
    ]
}
print(lineage_has_uploaded_code(lineage))  # True: share links and client portals refuse it
for pin in lineage_pins(lineage):
    print(pin.namespace, pin.plugin_version_id, pin.sha256[:19] + "...")
print(lineage_has_uploaded_code({}))  # False: built-in code only
# --8<-- [end:lineage]

assert lineage_has_uploaded_code(lineage)
assert not lineage_has_uploaded_code({})
assert [p.namespace for p in lineage_pins(lineage)] == ["t_k3vq7m_acme"]
