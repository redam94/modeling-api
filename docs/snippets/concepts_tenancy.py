"""Concepts: roles, the permission check, project selection and API tokens (mapi_spec only).

Included by ``docs/concepts/tenancy-and-auth.md``; run by ``conformance/tests/test_docs_snippets.py``.
"""

from __future__ import annotations

# --8<-- [start:can]
from mapi_spec.tenancy import Action, ProjectRole, ResourceKind, can

print(can(ProjectRole.EDITOR, Action.UPDATE, ResourceKind.SPEC))  # True
print(can(ProjectRole.EDITOR, Action.CREATE, ResourceKind.RUN))  # False: running costs money
print(can(ProjectRole.RUNNER, Action.CREATE, ResourceKind.RUN))  # True
print(can(ProjectRole.ADMIN, Action.UPDATE, ResourceKind.KILL_SWITCH))  # False: operators only
# --8<-- [end:can]

# --8<-- [start:context]
from mapi_spec.tenancy import AuthContext, TenantStatus, TokenPrincipal

token = TokenPrincipal(
    id="tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
    prefix="7k2m9q4x",
    owner_id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
    tenant_id="acme",
    project_id="growth",
    scopes=("score:geo-mmm-prod",),
)
ctx = AuthContext(
    principal=token,
    tenant_id="acme",
    project_id="growth",
    role=ProjectRole.ADMIN,  # the owner's role in the project, resolved now
    scopes=frozenset(token.scopes),
)
print(ctx.allows("run", "endpoint", resource_name="geo-mmm-prod"))  # True
print(ctx.allows("run", "endpoint", resource_name="other"))  # False: not in the scopes
print(ctx.allows("read", "dataset"))  # False: scopes narrow even an admin's token

frozen = ctx.model_copy(update={"tenant_status": TenantStatus.SUSPENDED})
print(frozen.allows("read", "endpoint", resource_name="geo-mmm-prod"))  # True: reads still work
print(frozen.allows("run", "endpoint", resource_name="geo-mmm-prod"))  # False
# --8<-- [end:context]

# --8<-- [start:project]
from mapi_spec.tenancy import resolve_project_scope

print(resolve_project_scope(header="acme/growth"))  # acme/growth
print(resolve_project_scope(header="growth", member_tenants=["acme"]))  # acme/growth
print(resolve_project_scope(token_binding=("acme", "growth")))  # acme/growth
# --8<-- [end:project]

# --8<-- [start:tokens]
from mapi_spec.errors import MapiError
from mapi_spec.tokens import generate_token, hash_token, parse_token, verify_token_hash

tok = generate_token()
text = str(tok)  # mapi_<8-char prefix>_<32-char secret><7-char checksum>
print(text.startswith("mapi_"), len(text))  # True 53
print(tok.display)  # mapi_<prefix>_…: the only form ever shown again

stored = hash_token(tok, pepper=b"server-side pepper")  # all the server keeps
print(verify_token_hash(text, stored, pepper=b"server-side pepper"))  # True

typo = text[:-1] + ("0" if text[-1] != "0" else "1")
try:
    parse_token(typo)
except MapiError as err:
    print(err.code.value)  # UNAUTHENTICATED: the checksum catches it without a lookup
# --8<-- [end:tokens]
