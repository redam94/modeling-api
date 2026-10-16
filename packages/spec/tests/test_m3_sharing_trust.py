"""M3 plot sharing (saved plots, renders, embeds: platform recipes only) and the trust
lifecycle (sign-up, verification, code-upload rule, deletion state machine, exports)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.jobs import RenderJob
from mapi_spec.plots import PlotSpec
from mapi_spec.sharing import (
    EMBED_TOKEN_RE,
    EmbedView,
    PlotEmbedCreate,
    PlotEmbedCreated,
    PlotRenderRequest,
    SavedPlotCreate,
    embeddable,
    generate_embed_token,
    is_platform_recipe,
)
from mapi_spec.tenancy import TrustTier
from mapi_spec.trust import (
    CUT_LINE_B_OPEN,
    DELETION_TRANSITIONS,
    BlocklistEmailPolicy,
    DeletionState,
    DisposableEmailPolicy,
    EmailVerify,
    SignupRequest,
    TenantDeletion,
    check_signup,
    email_domain,
    generate_verification_token,
    next_deletion_state,
    uploaded_code_allowed,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

PLOT = PlotSpec.model_json_schema()["examples"][0]


# ------------------------------------------------------------------------------- sharing


@pytest.mark.parametrize(
    ("recipe", "ok"),
    [
        ("core.ppc_fan@1.0.0", True),
        ("mmm.response_curves", True),
        ("ml.feature_importance@2", True),
        ("acme.custom_plot@1", False),
        ("t/acme/x.plot@1", False),
        ("core", False),
        (None, False),
        ("", False),
    ],
)
def test_platform_recipes(recipe: str | None, ok: bool) -> None:
    assert is_platform_recipe(recipe) is ok


def test_only_bundle_plots_of_platform_recipes_are_embeddable() -> None:
    assert embeddable("model_plot", "recipe", "mmm.response_curves@1.0.0")
    assert not embeddable("spec", "recipe", "core.ppc_fan@1.0.0")  # user-authored copy
    assert not embeddable("model_plot", "plugin", "core.ppc_fan@1.0.0")
    assert not embeddable("model_plot", "recipe", "acme.custom@1")


def test_embed_views_carry_inline_platform_plots_only() -> None:
    view = EmbedView.model_validate(EmbedView.model_json_schema()["examples"][0])
    assert all(d.kind == "inline_arrow" for d in view.plot.datasets.values())
    data = view.model_dump(mode="json")
    with pytest.raises(ValidationError, match="inline"):
        EmbedView.model_validate({**data, "plot": PLOT})  # a data_ref dataset
    authored = {
        **data["plot"],
        "provenance": {"author": "usr_x", "generated_at": "2026-10-15T09:30:00Z"},
    }
    with pytest.raises(ValidationError, match="platform recipes"):
        EmbedView.model_validate({**data, "plot": authored})


def test_embed_tokens_and_links() -> None:
    for _ in range(10):
        assert EMBED_TOKEN_RE.match(generate_embed_token())
    created = PlotEmbedCreated.model_validate(PlotEmbedCreated.model_json_schema()["examples"][0])
    assert created.token not in repr(created)
    with pytest.raises(ValidationError):
        PlotEmbedCreate(allowed_origins=["http://acme.example"])
    with pytest.raises(ValidationError):
        PlotEmbedCreate(allowed_origins=["https://acme.example/path"])
    with pytest.raises(ValidationError):
        PlotEmbedCreate(expires_in_days=366)


def test_saved_plots_and_render_requests_name_one_source() -> None:
    src = SavedPlotCreate.model_json_schema()["examples"][0]["source"]
    with pytest.raises(ValidationError):
        SavedPlotCreate(title="x")
    with pytest.raises(ValidationError):
        SavedPlotCreate.model_validate({"title": "x", "source": src, "spec": PLOT})
    assert SavedPlotCreate.model_validate({"title": "x", "spec": PLOT}).spec is not None
    with pytest.raises(ValidationError):
        PlotRenderRequest(format="pdf")
    with pytest.raises(ValidationError):
        PlotRenderRequest.model_validate({"source": src, "plot": PLOT})
    with pytest.raises(ValidationError):
        PlotRenderRequest.model_validate({"source": src, "width": 5000})
    job = RenderJob(format="pdf", width=1200, height=600)
    assert job.format == "pdf"


# --------------------------------------------------------------------------------- trust

SIGNUP = SignupRequest.model_json_schema()["examples"][0]


def test_signup_checks() -> None:
    assert check_signup(SignupRequest.model_validate(SIGNUP)) == "acme.example"
    for change, code in (
        ({"accepted_terms": False}, ErrorCode.TERMS_NOT_ACCEPTED),
        ({"terms_version": "2020-01"}, ErrorCode.TERMS_NOT_ACCEPTED),
        ({"email": "x@mailinator.com"}, ErrorCode.EMAIL_NOT_ALLOWED),
        ({"email": "x@eu.mailinator.com"}, ErrorCode.EMAIL_NOT_ALLOWED),
        ({"email": "x@YOPMAIL.com"}, ErrorCode.EMAIL_NOT_ALLOWED),
    ):
        with pytest.raises(MapiError) as exc:
            check_signup(SignupRequest.model_validate({**SIGNUP, **change}))
        assert exc.value.code is code, change
    with pytest.raises(ValidationError):
        SignupRequest.model_validate({**SIGNUP, "email": "not-an-address"})


def test_the_disposable_email_hook_is_pluggable() -> None:
    class Strict:
        def is_disposable(self, domain: str) -> bool:
            return not domain.endswith(".example")

    assert isinstance(Strict(), DisposableEmailPolicy)
    assert isinstance(BlocklistEmailPolicy(), DisposableEmailPolicy)
    req = SignupRequest.model_validate({**SIGNUP, "email": "x@gmail.com"})
    with pytest.raises(MapiError):
        check_signup(req, Strict())
    assert not BlocklistEmailPolicy(frozenset({"bad.io"})).is_disposable("notbad.io")
    assert BlocklistEmailPolicy(frozenset({"bad.io"})).is_disposable("x.bad.io.")


def test_email_domains_are_normalised() -> None:
    assert email_domain("Ana@Example.COM") == "example.com"
    assert email_domain("a@bücher.example") == "xn--bcher-kva.example"
    for bad in ("nope", "@x.com", "a@"):
        with pytest.raises(ValueError):
            email_domain(bad)


def test_verification_tokens() -> None:
    tok = generate_verification_token()
    assert EmailVerify(token=tok).token == tok
    with pytest.raises(ValidationError):
        EmailVerify(token="mev_short")


@pytest.mark.parametrize(
    ("tier", "verified", "public_code", "ok"),
    [
        (TrustTier.INTERNAL, False, False, True),
        (TrustTier.SELF_HOSTED_TRUSTED, False, False, True),
        (TrustTier.VERIFIED, False, False, False),
        (TrustTier.VERIFIED, True, False, True),
        (TrustTier.PUBLIC, True, False, False),  # cut line A: built-ins only
        (TrustTier.PUBLIC, False, True, False),
        (TrustTier.PUBLIC, True, True, True),  # cut line B, verified identity
    ],
)
def test_uploaded_code_needs_identity_verification(
    tier: TrustTier, verified: bool, public_code: bool, ok: bool
) -> None:
    allowed, reason = uploaded_code_allowed(
        tier, identity_verified=verified, public_code_enabled=public_code
    )
    assert allowed is ok and reason
    assert CUT_LINE_B_OPEN is False


EVENTS = ("cancel", "start", "store_failed", "all_done", "retry")


@pytest.mark.parametrize(("state", "event"), list(itertools.product(DeletionState, EVENTS)))
def test_deletion_state_machine_is_exhaustive(state: DeletionState, event: str) -> None:
    target = DELETION_TRANSITIONS.get((state, event))
    if target is None:
        with pytest.raises(MapiError) as exc:
            next_deletion_state(state, event)  # type: ignore[arg-type]
        assert exc.value.code is ErrorCode.CONFLICT
    else:
        assert next_deletion_state(state, event) is target  # type: ignore[arg-type]


def test_terminal_deletion_states() -> None:
    for terminal in (DeletionState.PURGED, DeletionState.CANCELLED):
        assert not [k for k in DELETION_TRANSITIONS if k[0] is terminal]


def test_deletion_progress() -> None:
    d = TenantDeletion.model_validate(TenantDeletion.model_json_schema()["examples"][0])
    assert d.fraction_done == pytest.approx(2 / 5)
    assert set(d.retained) == {"audit", "cost_ledger"}
    data: dict[str, Any] = d.model_dump(mode="json")
    with pytest.raises(ValidationError):
        TenantDeletion.model_validate({**data, "purge_after": "2026-01-01T00:00:00Z"})
    purged = TenantDeletion.model_validate({**data, "state": "purged", "stores": []})
    assert purged.fraction_done == 1.0
    assert datetime(2026, 1, 1, tzinfo=UTC) < purged.purge_after


def test_run_kind_values_fit_the_database_column() -> None:
    assert all(len(k.value) <= 16 for k in RunKind)  # runs.kind is VARCHAR(16)
    assert RunKind.TRANSLATE_PRIORS.value == "translate_priors"
    assert not RunKind.TRANSLATE_PRIORS.is_parent
