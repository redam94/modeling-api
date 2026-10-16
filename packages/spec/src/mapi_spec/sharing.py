"""Saved plots, static rendering and embeds (plan §8.2 Plots, §10.3 items 4-5; M3).

* **Saved plots** (``POST /v1/plots``) keep a plot under a name: either a reference to a
  plot a model version rendered (a *platform recipe* or a plugin plot) or a user-authored
  :class:`~mapi_spec.plots.PlotSpec` (sanitized on save and on every read).
* **Static rendering** (``POST /v1/plots:render``) turns a plot into PNG, SVG or PDF in the
  sandbox (vl-convert for Vega-Lite, kaleido for Plotly): a ``render`` run.
* **Embeds** (``POST /v1/plots/{plot_id}/embeds``) mint a tokenised, read-only, expiring
  link for BI tools, Slack and Notion. Plan §10.3 item 4: shared views render **platform
  recipes only** - never plugin- or user-authored specs - because a sandboxed iframe can
  still navigate itself; :func:`embeddable` decides. The public ``GET /v1/embeds/{token}``
  returns an :class:`EmbedView` with every dataset inlined (no URLs).

Embed tokens are ``mpe_<40 base32 characters>`` (200 random bits); the server stores only
an HMAC (like API tokens) and shows the URL once.
"""

from __future__ import annotations

import re
import secrets
from typing import Final, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, TenantId, dto_examples
from mapi_spec.plots import PlotSpec

__all__ = [
    "EMBED_TOKEN_RE",
    "MAX_EMBED_DAYS",
    "PLATFORM_RECIPE_NAMESPACES",
    "EmbedView",
    "ModelPlotRef",
    "PlotEmbed",
    "PlotEmbedCreate",
    "PlotEmbedCreated",
    "PlotRenderRequest",
    "RenderFormat",
    "SavedPlot",
    "SavedPlotCreate",
    "embeddable",
    "generate_embed_token",
    "is_platform_recipe",
]

PLATFORM_RECIPE_NAMESPACES: Final = ("core", "mmm", "ml", "ts", "choice")
"""Plot recipe namespaces shipped by the platform (``mapi_plots``): the only embeddable plots."""
MAX_EMBED_DAYS: Final = 365
EMBED_TOKEN_RE: Final = re.compile(r"^mpe_[0-9a-hjkmnp-tv-z]{40}$")
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

RenderFormat = Literal["png", "svg", "pdf"]


def generate_embed_token() -> str:
    """A new embed token (shown once; store its keyed digest)."""
    return "mpe_" + "".join(secrets.choice(_ALPHABET) for _ in range(40))


def is_platform_recipe(recipe: str | None) -> bool:
    """``core.ppc_fan@1.0.0`` → True; plugin or unknown namespaces → False."""
    if not recipe:
        return False
    name = recipe.split("@", 1)[0]
    ns, dot, rest = name.partition(".")
    return bool(dot and rest) and ns in PLATFORM_RECIPE_NAMESPACES


class ModelPlotRef(RequestModel):
    """A plot a model version rendered (``GET /v1/models/{id}/versions/{v}/plots``)."""

    model_id: Id
    version: int = Field(..., ge=1)
    plot_id: str = Field(..., pattern=r"^[A-Za-z0-9_.\-]{1,128}$")


class SavedPlotCreate(RequestModel):
    """``POST /v1/plots``: exactly one of ``source`` (a model version's plot) or ``spec``
    (a user-authored plot, sanitized first; never embeddable)."""

    title: str = Field(..., min_length=1, max_length=300)
    description: str | None = Field(None, max_length=4000)
    source: ModelPlotRef | None = None
    spec: PlotSpec | None = None

    model_config = dto_examples(
        {
            "title": "EU response curves (v3)",
            "source": {
                "model_id": "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0",
                "version": 3,
                "plot_id": "mmm.response_curves",
            },
        }
    )

    @model_validator(mode="after")
    def _one(self) -> SavedPlotCreate:
        if (self.source is None) == (self.spec is None):
            raise ValueError("give exactly one of 'source' or 'spec'")
        return self


class SavedPlot(ResponseModel):
    """A saved plot. ``spec`` is the sanitized PlotSpec (on ``GET /v1/plots/{plot_id}``;
    omitted in lists). ``embeddable`` is :func:`embeddable` of this plot."""

    id: Id = Field(..., pattern=r"^plt_[0-9a-z]{26}$")
    tenant_id: TenantId
    project_id: TenantId
    title: str
    description: str | None = None
    kind: Literal["vega-lite", "plotly"]
    source_kind: Literal["model_plot", "spec"]
    source: ModelPlotRef | None = None
    recipe: str | None = Field(None, max_length=128, description="provenance.recipe, if any")
    provenance: Literal["recipe", "plugin", "author"]
    embeddable: bool
    spec: PlotSpec | None = None
    created_by: str | None = Field(None, max_length=128)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    model_config = dto_examples(
        {
            "id": "plt_01jaf3n1p2q3r4s5t6v7w8x9y0",
            "tenant_id": "acme",
            "project_id": "growth",
            "title": "EU response curves (v3)",
            "kind": "vega-lite",
            "source_kind": "model_plot",
            "source": SavedPlotCreate.model_json_schema()["examples"][0]["source"],
            "recipe": "mmm.response_curves@1.0.0",
            "provenance": "recipe",
            "embeddable": True,
            "created_by": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
            "created_at": EXAMPLE_TS,
            "updated_at": EXAMPLE_TS,
        }
    )


def embeddable(source_kind: str, provenance: str, recipe: str | None) -> bool:
    """Plan §10.3 item 4: only plots a model version rendered from a *platform recipe*."""
    return source_kind == "model_plot" and provenance == "recipe" and is_platform_recipe(recipe)


class PlotRenderRequest(RequestModel):
    """``POST /v1/plots:render``: exactly one of ``plot`` (an inline spec, sanitized),
    ``saved_plot_id`` or ``source``. ``width``/``height`` are CSS pixels before ``scale``.
    Send ``Prefer: wait=N`` to get the file links in the response."""

    plot: PlotSpec | None = None
    saved_plot_id: Id | None = Field(None, pattern=r"^plt_[0-9a-z]{26}$")
    source: ModelPlotRef | None = None
    format: RenderFormat = "png"
    scale: float = Field(2.0, gt=0, le=8)
    width: int | None = Field(None, ge=16, le=4096)
    height: int | None = Field(None, ge=16, le=4096)

    model_config = dto_examples(
        {"saved_plot_id": "plt_01jaf3n1p2q3r4s5t6v7w8x9y0", "format": "pdf", "scale": 1},
        {
            "source": SavedPlotCreate.model_json_schema()["examples"][0]["source"],
            "format": "png",
            "width": 1200,
            "height": 600,
        },
    )

    @model_validator(mode="after")
    def _one(self) -> PlotRenderRequest:
        if sum(x is not None for x in (self.plot, self.saved_plot_id, self.source)) != 1:
            raise ValueError("give exactly one of 'plot', 'saved_plot_id' or 'source'")
        return self


class PlotEmbedCreate(RequestModel):
    """``POST /v1/plots/{plot_id}/embeds``. ``allowed_origins`` (https origins) become the
    embed page's ``frame-ancestors``; empty allows any site to frame it."""

    expires_in_days: int = Field(30, ge=1, le=MAX_EMBED_DAYS)
    label: str | None = Field(None, max_length=200)
    allowed_origins: list[str] = Field(default_factory=list, max_length=10)

    model_config = dto_examples(
        {
            "expires_in_days": 90,
            "label": "Q4 board deck",
            "allowed_origins": ["https://acme.notion.site"],
        }
    )

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v: list[str]) -> list[str]:
        for o in v:
            if not re.fullmatch(r"https://[a-z0-9.-]{1,253}(?::[0-9]{1,5})?", o):
                raise ValueError(f"{o[:80]!r} is not an https origin (scheme://host[:port])")
        return v


class PlotEmbed(ResponseModel):
    """An embed link as listed (never its token)."""

    id: Id = Field(..., pattern=r"^emb_[0-9a-z]{26}$")
    plot_id: Id
    tenant_id: TenantId
    project_id: TenantId
    label: str | None = None
    display: str = Field(..., description="Redacted token, mpe_<first 6>…")
    allowed_origins: list[str] = Field(default_factory=list)
    status: Literal["active", "expired", "revoked"]
    views: int = Field(0, ge=0)
    last_viewed_at: AwareDatetime | None = None
    created_by: str | None = Field(None, max_length=128)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "id": "emb_01jaf3p1q2r3s4t5v6w7x8y9z0",
            "plot_id": "plt_01jaf3n1p2q3r4s5t6v7w8x9y0",
            "tenant_id": "acme",
            "project_id": "growth",
            "label": "Q4 board deck",
            "display": "mpe_7k2m9q…",
            "allowed_origins": ["https://acme.notion.site"],
            "status": "active",
            "views": 12,
            "created_at": EXAMPLE_TS,
            "expires_at": "2027-01-13T09:30:00Z",
        }
    )


class PlotEmbedCreated(ResponseModel):
    """``201`` of ``POST …/embeds``: the only time the URL (with its token) is returned."""

    embed: PlotEmbed
    url: str = Field(..., description="Public read-only URL; paste it into the BI tool")
    token: str = Field(..., pattern=EMBED_TOKEN_RE.pattern)

    model_config = dto_examples(
        {
            "embed": PlotEmbed.model_json_schema()["examples"][0],
            "url": "https://plots.mapi.example/e/mpe_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz",
            "token": "mpe_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz",
        }
    )

    def __repr__(self) -> str:
        return f"PlotEmbedCreated(embed={self.embed.id!r}, token=<redacted>)"

    __str__ = __repr__


class EmbedView(ResponseModel):
    """``GET /v1/embeds/{token}`` (public, rate-limited, cacheable until ``expires_at``):
    a platform-recipe plot with every dataset inlined. Expired or revoked tokens answer
    ``410 LINK_EXPIRED``; unknown ones ``404``."""

    title: str
    caption_plain: str | None = None
    plot: PlotSpec
    generated_at: AwareDatetime
    expires_at: AwareDatetime
    allowed_origins: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="The embed's PlotEmbedCreate.allowed_origins: sites that may frame the "
        "embed page (empty: any). The embed page refuses to render elsewhere.",
    )
    image_url: str | None = Field(
        None,
        max_length=4096,
        description="Short-lived signed URL of a static PNG of the plot (the embed page's "
        "fallback link), once the sandbox has rendered one",
    )

    model_config = dto_examples(
        {
            "title": "EU response curves (v3)",
            "plot": {
                **PlotSpec.model_json_schema()["examples"][0],
                "datasets": {"fan": {"kind": "inline_arrow", "data": "QVJST1cx", "rows": 2}},
            },
            "generated_at": EXAMPLE_TS,
            "expires_at": "2027-01-13T09:30:00Z",
            "allowed_origins": ["https://acme.notion.site"],
            "image_url": "https://storage.googleapis.com/mapi-prod-bundles/t/acme/embeds/"
            "emb_01jaf3p1q2r3s4t5v6w7x8y9z0/plot.png?X-Goog-Signature=…",
        }
    )

    @model_validator(mode="after")
    def _inline_recipe(self) -> EmbedView:
        if any(d.kind != "inline_arrow" for d in self.plot.datasets.values()):
            raise ValueError("embedded plots carry every dataset inline")
        if not is_platform_recipe(self.plot.provenance.recipe):
            raise ValueError("only platform recipes can be embedded")
        return self
