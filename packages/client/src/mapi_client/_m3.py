"""Synchronous M3 resources (guided mode and integrations, v0.4).

``Client.guided``, ``Client.endpoints``, ``Client.webhooks``, the saved-plot, render and
embed methods of ``Client.plots``, the trust lifecycle on ``Client.tenants`` and
``Client.admin``, and :class:`RemoteEndpoint` (``Client.endpoint(alias)``).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

import httpx
from mapi_spec.api import (
    MAX_PREFER_WAIT_SECONDS,
    ArtifactLink,
    DetectRolesRequest,
    EmailVerify,
    EmbedView,
    Endpoint,
    EndpointRevision,
    EndpointRollback,
    EndpointSchema,
    EndpointScoreCall,
    EndpointValidation,
    GdprExport,
    GdprExportRequest,
    GuidedAnswers,
    GuidedCompile,
    GuidedDraft,
    GuidedTemplate,
    PlotEmbed,
    PlotEmbedCreate,
    PlotEmbedCreated,
    PlotRender,
    PlotSpec,
    PriorTranslation,
    RoleSuggestions,
    SavedPlot,
    ScoreCallResult,
    SignupAccepted,
    SignupRequest,
    SignupReview,
    SignupReviewDecision,
    SnippetSet,
    TenantDeletion,
    TenantDeletionRequest,
    VerificationResult,
    VerificationStatus,
    VerificationUpdate,
    Webhook,
    WebhookCreated,
    WebhookDelivery,
)
from mapi_spec.calibration import BusinessPriors, LiftTest
from mapi_spec.endpoints import EvaluationPolicy
from mapi_spec.guided import ColumnProfile
from mapi_spec.resources import ResourceClass
from mapi_spec.snippets import SnippetLanguage
from mapi_spec.spec import ScoreRequest
from mapi_spec.trust import ExportPart, SignupReviewState, VerificationMethod
from mapi_spec.webhooks import DeliveryStatus, WebhookEventType
from pydantic import JsonValue

from mapi_client import _calls_m3 as m3
from mapi_client._common import ScoreResult, run_error, tree_output
from mapi_client._m3_common import (
    Deadline,
    UrlSink,
    as_model,
    conform,
    export_done,
    promote_body,
    render_body,
    render_link,
    result_dataset,
    saved_plot_body,
    score_call,
    translation_body,
    webhook_create_body,
    webhook_update_body,
)
from mapi_client.errors import TransportError, raise_for_response
from mapi_client.pagination import Paginator
from mapi_client.transfer import datatree_from_bytes, require_xarray, xarray_available
from mapi_client.xrjson import is_dataset

if TYPE_CHECKING:
    import xarray as xr

    from mapi_client._sync import Client

__all__ = [
    "AdminTrustMixin",
    "Endpoints",
    "Guided",
    "PlotSharingMixin",
    "RemoteEndpoint",
    "TenantTrustMixin",
    "Webhooks",
    "download_url",
    "signup",
    "verify_email",
]

Payload = Any
"""``xr.Dataset`` | XrJSON mapping | :class:`~mapi_spec.xrjson.XrJsonDataset` |
``dataset://`` reference."""


class _Resource:
    def __init__(self, client: Client) -> None:
        self._c = client


def _poll[T](
    client: Client,
    fetch: Callable[[], T],
    done: Callable[[T], bool],
    *,
    what: str,
    timeout: float | None,
    poll_interval: float,
    max_poll_interval: float = 10.0,
) -> T:
    """Poll ``fetch()`` until ``done(value)`` (``TimeoutError`` after ``timeout`` s)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    delay = poll_interval
    while True:
        value = fetch()
        if done(value):
            return value
        if deadline is not None and time.monotonic() + delay > deadline:
            raise TimeoutError(f"{what} is not done after {timeout} s")
        client._sleep(delay)
        delay = min(delay * 1.5, max_poll_interval)


def _keyed[T](
    client: Client,
    send: Callable[[int], T],
    done: Callable[[T], bool],
    *,
    wait: int,
    timeout: float | None,
    poll_interval: float,
    what: str,
) -> T:
    """Repeat a keyed ``Prefer: wait`` submission until ``done(result)``.

    ``send(wait)`` sends the request with the same ``Idempotency-Key`` every time, so only
    the first creates a run; a read timeout while the server holds the answer counts as
    "still running".
    """
    budget = Deadline.start(wait, timeout)
    while True:
        this_wait = budget.next_wait()
        result: T | None
        try:
            result = send(this_wait)
        except TransportError as exc:
            if not isinstance(exc.__cause__, httpx.ReadTimeout):
                raise
            result = None
        if result is not None and done(result):
            return result
        if budget.expired:
            raise TimeoutError(f"{what} is still running after {timeout} s")
        if this_wait < 1:
            client._sleep(poll_interval)


# =================================================================================== guided


class Guided(_Resource):
    """Guided mode (v0.4): templates, role detection, drafts, "Run exact" compilation and
    business-prior translation."""

    def templates(self, *, limit: int | None = None) -> Paginator[GuidedTemplate]:
        return self._c.page_iter(lambda cur: m3.list_guided_templates(limit, cur))

    def template(self, template_id: str) -> GuidedTemplate:
        return self._c.send(m3.get_guided_template(template_id))

    def detect_roles(
        self,
        template_id: str,
        *,
        dataset: str | None = None,
        columns: Sequence[ColumnProfile | Mapping[str, Any]] | None = None,
    ) -> RoleSuggestions:
        """Suggest a column for every question, from a stored dataset version
        (``dataset://project/name@vN``) or column profiles (exactly one)."""
        body = DetectRolesRequest.model_validate(
            {
                "template_id": template_id,
                "dataset": dataset,
                "columns": None
                if columns is None
                else [as_model(ColumnProfile, c) for c in columns],
            }
        )
        return self._c.send(m3.detect_roles(body))

    def draft(
        self, body: GuidedAnswers | Mapping[str, Any] | None = None, **fields: Any
    ) -> GuidedDraft:
        """The ingest mapping, ``ModelSpec`` and YAML for a template's answers (stores
        nothing). Give a :class:`GuidedAnswers` (or mapping) and/or its fields
        (``template_id=``, ``name=``, ``dataset=``, ``answers=``, ...)."""
        base = (
            body.model_dump(mode="json", by_alias=True, exclude_unset=True)
            if isinstance(body, GuidedAnswers)
            else dict(body or {})
        )
        base.update(fields)
        return self._c.send(m3.create_guided_draft(GuidedAnswers.model_validate(base)))

    def compile(self, request: GuidedCompile | Mapping[str, Any]) -> ScoreRequest:
        """The ``ScoreRequest`` a what-if scenario, forecast or preset stands for."""
        body = (
            request
            if isinstance(request, GuidedCompile)
            else GuidedCompile.model_validate(dict(request))
        )
        return self._c.send(m3.compile_guided_request(body))

    def translate_priors(
        self,
        business_priors: BusinessPriors | Mapping[str, Any],
        *,
        spec_id: str | None = None,
        revision: int | None = None,
        document: Mapping[str, JsonValue] | None = None,
        dataset: str | None = None,
        lift_tests: Sequence[LiftTest | Mapping[str, Any]] = (),
        draws: int = 500,
        seed: int | None = None,
        resource_class: ResourceClass | str | None = None,
        idempotency_key: str | None = None,
    ) -> PriorTranslation:
        """Start a sandboxed ``translate_priors`` run (a saved spec revision or an unsaved
        ``document``); see :meth:`wait_prior_translation`."""
        body = translation_body(
            business_priors,
            spec_id=spec_id,
            revision=revision,
            document=document,
            dataset=dataset,
            lift_tests=lift_tests,
            draws=draws,
            seed=seed,
            resource_class=resource_class,
        )
        return self._c.send(m3.create_prior_translation(body, idempotency_key))

    def prior_translation(self, run_id: str) -> PriorTranslation:
        return self._c.send(m3.get_prior_translation(run_id))

    def wait_prior_translation(
        self,
        run_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
        raise_on_failure: bool = True,
    ) -> PriorTranslation:
        """Poll until the translation run is done; raises its error if it failed."""
        got: PriorTranslation = _poll(
            self._c,
            lambda: self.prior_translation(run_id),
            lambda t: t.done or t.run.state.terminal,
            what=f"prior translation {run_id}",
            timeout=timeout,
            poll_interval=poll_interval,
        )
        err = run_error(got.run) if raise_on_failure else None
        if err is not None:
            raise err
        return got

    def preview_priors(
        self,
        business_priors: BusinessPriors | Mapping[str, Any],
        *,
        timeout: float | None = 600.0,
        poll_interval: float = 1.0,
        **options: Any,
    ) -> PriorTranslation:
        """:meth:`translate_priors` and wait for the priors and their preview."""
        started = self.translate_priors(business_priors, **options)
        if started.done:
            err = run_error(started.run)
            if err is not None:
                raise err
            return started
        return self.wait_prior_translation(
            started.run.id, timeout=timeout, poll_interval=poll_interval
        )


# ================================================================================ endpoints


class Endpoints(_Resource):
    """Endpoints and aliases (v0.4): promotion through the scorecard gate, history,
    rollback, schema, validation, scoring and snippets."""

    def list(self, *, limit: int | None = None) -> Paginator[Endpoint]:
        return self._c.page_iter(lambda cur: m3.list_endpoints(limit, cur))

    def get(self, alias: str) -> Endpoint:
        return self._c.send(m3.get_endpoint(alias))

    def promote(
        self,
        alias: str,
        model: str | None = None,
        *,
        model_version_id: str | None = None,
        override: bool = False,
        reason: str | None = None,
        expected_revision: int | None = None,
        evaluation_policy: EvaluationPolicy | Mapping[str, Any] | None = None,
        description: str | None = None,
    ) -> Endpoint:
        """Create the alias or point it at another version (``model://p/name@vN`` or
        ``model_version_id``). Gated on the scorecard: ``409 OVERRIDE_REQUIRED`` needs
        ``override=True`` with a ``reason`` from a signed-in project admin;
        ``PROMOTION_BLOCKED`` cannot be overridden. ``expected_revision=0``: must be new."""
        body = promote_body(
            model,
            model_version_id=model_version_id,
            override=override,
            reason=reason,
            expected_revision=expected_revision,
            evaluation_policy=evaluation_policy,
            description=description,
        )
        return self._c.send(m3.promote_endpoint(alias, body))

    def delete(self, alias: str) -> None:
        """Delete the alias (the model versions stay)."""
        self._c.send(m3.delete_endpoint(alias))

    def history(self, alias: str, *, limit: int | None = None) -> Paginator[EndpointRevision]:
        """Promotions and rollbacks, newest first."""
        return self._c.page_iter(lambda cur: m3.list_endpoint_history(alias, limit, cur))

    def rollback(self, alias: str, reason: str, *, to_revision: int | None = None) -> Endpoint:
        """Point the alias back at an earlier revision's version (default: the previous)."""
        body = EndpointRollback(reason=reason, to_revision=to_revision)
        return self._c.send(m3.rollback_endpoint(alias, body))

    def schema(self, alias: str) -> EndpointSchema:
        """What to send: the input schema frozen at promotion and an example payload."""
        return self._c.send(m3.get_endpoint_schema(alias))

    def validate(
        self, alias: str, call: EndpointScoreCall | Mapping[str, Any] | None = None, **fields: Any
    ) -> EndpointValidation:
        """Static checks of a score call without running it (``valid``/``issues``)."""
        return self._c.send(m3.validate_endpoint_request(alias, score_call(call, fields)))

    def submit(
        self,
        alias: str,
        call: EndpointScoreCall | Mapping[str, Any] | None = None,
        *,
        wait: int | None = None,
        idempotency_key: str | None = None,
        **fields: Any,
    ) -> ScoreCallResult:
        """One ``POST …/score`` (``200`` done or ``202`` running): no waiting loop."""
        body = score_call(call, fields)
        key = idempotency_key or str(uuid.uuid4())
        return self._c.send(m3.score_endpoint(alias, body, wait=wait, idempotency_key=key))

    def score(
        self,
        alias: str,
        call: EndpointScoreCall | Mapping[str, Any] | None = None,
        *,
        wait: int = MAX_PREFER_WAIT_SECONDS,
        timeout: float | None = None,
        idempotency_key: str | None = None,
        download: bool = True,
        raise_on_failure: bool = True,
        poll_interval: float = 1.0,
        **fields: Any,
    ) -> ScoreResult:
        """Score the endpoint's current version and wait for the result.

        ``call`` is an :class:`EndpointScoreCall` (or mapping, e.g. the example of
        :meth:`schema`); keyword ``fields`` override it (``mode=``, ``horizon=``,
        ``data=xr.Dataset`` - sent inline as XrJSON). ``Prefer: wait=<wait>`` is sent and
        the call is repeated with the same ``Idempotency-Key`` until the run is done or
        ``timeout`` passes. With the ``xarray`` extra and ``download=True`` the result
        DataTree is fetched (CRC32C-verified) into :attr:`ScoreResult.tree`.
        """
        body = score_call(call, fields)
        key = idempotency_key or str(uuid.uuid4())
        res: ScoreCallResult = _keyed(
            self._c,
            lambda w: self._c.send(m3.score_endpoint(alias, body, wait=w, idempotency_key=key)),
            lambda r: r.done or r.run.state.terminal,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            what=f"score call {key} on {alias}",
        )
        if raise_on_failure:
            err = run_error(res.run)
            if err is not None:
                raise err
        tree = None
        link = tree_output(list(res.outputs))
        if download and link is not None and xarray_available():
            tree = datatree_from_bytes(self._c.download(link))
        return ScoreResult.from_call(res, tree)

    def snippets(
        self,
        alias: str,
        languages: Sequence[SnippetLanguage | str] | None = None,
        *,
        base_url: str | None = None,
    ) -> SnippetSet:
        """Ready-to-run code (Python, TypeScript, curl, R, Apps Script, notebook)."""
        return self._c.send(m3.list_endpoint_snippets(alias, languages, base_url))


class RemoteEndpoint:
    """One endpoint as an object: ``Client().endpoint("geo-mmm-prod").score(ds)``.

    ``score`` sends an ``xr.Dataset`` (or an XrJSON mapping, or a ``dataset://`` reference)
    and returns the scored ``xr.Dataset`` of one result group (``summary`` by default;
    ``draws`` when ``output={"draws": n}`` is requested). Needs the ``xarray`` extra.
    """

    def __init__(self, client: Client, alias: str) -> None:
        m3.get_endpoint(alias)  # validates the alias as a path segment
        self._c = client
        self.alias = alias
        self._schema: EndpointSchema | None = None

    def __repr__(self) -> str:
        return f"RemoteEndpoint({self.alias!r}, project={self._c.project!r})"

    def info(self) -> Endpoint:
        """The endpoint and the version it serves now."""
        return self._c.endpoints.get(self.alias)

    def schema(self) -> EndpointSchema:
        """What to send (fetched afresh; also kept to conform later payloads)."""
        self._schema = self._c.endpoints.schema(self.alias)
        return self._schema

    def _conformed(self, data: Payload | None, enabled: bool) -> Payload | None:
        """An ``xr.Dataset`` transposed to the endpoint's input dim order."""
        if not enabled or not is_dataset(data):
            return data
        schema = self._schema or self.schema()
        return conform(data, schema.snapshot.input_schema)

    def history(self, *, limit: int | None = None) -> Paginator[EndpointRevision]:
        return self._c.endpoints.history(self.alias, limit=limit)

    def snippets(self, languages: Sequence[SnippetLanguage | str] | None = None) -> SnippetSet:
        return self._c.endpoints.snippets(self.alias, languages)

    def validate(
        self, data: Payload | None = None, *, conform: bool = True, **fields: Any
    ) -> EndpointValidation:
        """Static checks of scoring ``data`` (and any call fields) without running it."""
        data = self._conformed(data, conform)
        return self._c.endpoints.validate(self.alias, None, data=data, **fields)

    def score_result(self, data: Payload | None = None, **options: Any) -> ScoreResult:
        """:meth:`Endpoints.score` for this endpoint (the run, links and tree)."""
        return self._c.endpoints.score(self.alias, None, data=data, **options)

    def score(
        self,
        data: Payload | None = None,
        *,
        group: str = "summary",
        wait: int = MAX_PREFER_WAIT_SECONDS,
        timeout: float | None = None,
        conform: bool = True,
        **fields: Any,
    ) -> xr.Dataset:
        """Score ``data`` and return one group of the result as an ``xr.Dataset``.

        ``fields`` are :class:`EndpointScoreCall` fields (``mode``, ``horizon``,
        ``scenario``, ``output``, ``interventions``, ...). ``group`` picks the result group:
        ``summary`` (a ``stat`` dim: ``mean``, ``hdi_90_lower``, ...), ``draws`` (a
        ``sample`` dim), ``delta/summary`` for counterfactuals. With ``conform`` (default)
        a dataset's variables are transposed to the endpoint's input dim order first (the
        schema is fetched once).
        """
        require_xarray()
        data = self._conformed(data, conform)
        result = self._c.endpoints.score(
            self.alias, None, data=data, wait=wait, timeout=timeout, download=True, **fields
        )
        if result.tree is None:
            raise ValueError(f"score run {result.run.id} returned no result tree")
        return result_dataset(result.tree, group)


def signup(
    client: Client,
    email: str,
    name: str,
    *,
    accepted_terms: bool,
    organisation: str | None = None,
    terms_version: str | None = None,
    challenge: str | None = None,
) -> SignupAccepted:
    fields: dict[str, Any] = {
        "email": email,
        "name": name,
        "organisation": organisation,
        "accepted_terms": accepted_terms,
        "challenge": challenge,
    }
    if terms_version is not None:
        fields["terms_version"] = terms_version
    return client.send(m3.signup(SignupRequest.model_validate(fields)))


def verify_email(client: Client, token: str) -> VerificationResult:
    return client.send(m3.verify_email(EmailVerify(token=token)))


# ================================================================================= webhooks


class Webhooks(_Resource):
    """Outgoing webhooks of the selected project (v0.4). The signing secret is returned
    once, by :meth:`create` and :meth:`rotate_secret`; verify deliveries with
    :func:`mapi_client.webhooks.verify`."""

    def list(self, *, limit: int | None = None) -> Paginator[Webhook]:
        return self._c.page_iter(lambda cur: m3.list_webhooks(limit, cur))

    def create(
        self,
        url: str,
        events: Sequence[WebhookEventType | str],
        *,
        description: str | None = None,
        active: bool = True,
    ) -> WebhookCreated:
        """Add a webhook. ``WebhookCreated.secret`` is the only copy of the secret.

        Never resent after a lost response (that could create two webhooks)."""
        body = webhook_create_body(url, events, description, active)
        return self._c.send(m3.create_webhook(body))

    def get(self, webhook_id: str) -> Webhook:
        return self._c.send(m3.get_webhook(webhook_id))

    def update(
        self,
        webhook_id: str,
        *,
        url: str | None = None,
        events: Sequence[WebhookEventType | str] | None = None,
        description: str | None = None,
        active: bool | None = None,
    ) -> Webhook:
        """Change the URL, events, description or ``active`` (re-activating resets the
        failure count)."""
        body = webhook_update_body(url=url, events=events, description=description, active=active)
        return self._c.send(m3.update_webhook(webhook_id, body))

    def delete(self, webhook_id: str) -> None:
        self._c.send(m3.delete_webhook(webhook_id))

    def rotate_secret(self, webhook_id: str) -> WebhookCreated:
        """A new secret (returned once); the old one keeps signing for 24 hours."""
        return self._c.send(m3.rotate_webhook_secret(webhook_id))

    def deliveries(
        self,
        webhook_id: str,
        *,
        status: DeliveryStatus | str | None = None,
        limit: int | None = None,
    ) -> Paginator[WebhookDelivery]:
        """The delivery log, newest first. ``response_excerpt`` is untrusted text."""
        return self._c.page_iter(
            lambda cur: m3.list_webhook_deliveries(webhook_id, limit, cur, status=status)
        )

    def replay(self, webhook_id: str, delivery_id: str) -> WebhookDelivery:
        """Send a delivery's event again (a new delivery with ``replay_of`` set)."""
        return self._c.send(m3.replay_webhook_delivery(webhook_id, delivery_id))


# ==================================================================== plots: save and share


class PlotSharingMixin(_Resource):
    """Saved plots, static renders and embed links on ``Client.plots`` (v0.4)."""

    def save(
        self,
        title: str,
        *,
        model_id: str | None = None,
        version: int | None = None,
        plot_id: str | None = None,
        spec: PlotSpec | Mapping[str, Any] | None = None,
        description: str | None = None,
    ) -> SavedPlot:
        """Save a model version's plot (``model_id``, ``version``, ``plot_id``) or a
        user-authored ``spec`` (sanitized on save)."""
        body = saved_plot_body(
            title,
            model_id=model_id,
            version=version,
            plot_id=plot_id,
            spec=spec,
            description=description,
        )
        return self._c.send(m3.create_saved_plot(body))

    def saved(self, *, limit: int | None = None) -> Paginator[SavedPlot]:
        return self._c.page_iter(lambda cur: m3.list_saved_plots(limit, cur))

    def get_saved(self, plot_id: str) -> SavedPlot:
        return self._c.send(m3.get_saved_plot(plot_id))

    def delete_saved(self, plot_id: str) -> None:
        """Delete a saved plot and revoke its embed links."""
        self._c.send(m3.delete_saved_plot(plot_id))

    def render(
        self,
        plot: PlotSpec | Mapping[str, Any] | None = None,
        *,
        saved_plot_id: str | None = None,
        model_id: str | None = None,
        version: int | None = None,
        plot_id: str | None = None,
        format: Literal["png", "svg", "pdf"] = "png",
        scale: float = 2.0,
        width: int | None = None,
        height: int | None = None,
        wait: int = MAX_PREFER_WAIT_SECONDS,
        timeout: float | None = None,
        idempotency_key: str | None = None,
        raise_on_failure: bool = True,
        poll_interval: float = 1.0,
    ) -> PlotRender:
        """Render one plot (a spec, a saved plot or a model version's plot) to PNG, SVG or
        PDF in the sandbox and wait for it; :meth:`download_render` fetches the file."""
        body = render_body(
            plot,
            saved_plot_id=saved_plot_id,
            model_id=model_id,
            version=version,
            plot_id=plot_id,
            format=format,
            scale=scale,
            width=width,
            height=height,
        )
        key = idempotency_key or str(uuid.uuid4())
        render: PlotRender = _keyed(
            self._c,
            lambda w: self._c.send(m3.render_plot(body, wait=w, idempotency_key=key)),
            lambda r: r.done or r.run.state.terminal,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            what=f"render {key}",
        )
        err = run_error(render.run) if raise_on_failure else None
        if err is not None:
            raise err
        return render

    def render_status(self, run_id: str) -> PlotRender:
        return self._c.send(m3.get_plot_render(run_id))

    def render_output(self, render: PlotRender) -> ArtifactLink:
        """The signed link of a finished render's file (its ``name`` is the file name)."""
        return render_link(render)

    @overload
    def download_render(self, render: PlotRender, dest: None = None) -> bytes: ...
    @overload
    def download_render(self, render: PlotRender, dest: str | os.PathLike[str]) -> Path: ...
    def download_render(
        self, render: PlotRender, dest: str | os.PathLike[str] | None = None
    ) -> bytes | Path:
        """The rendered file (CRC32C-verified): bytes, or the path when ``dest`` is given."""
        link = render_link(render)
        if dest is None:
            return self._c.download(link)
        return self._c.download(link, dest)

    def create_embed(
        self,
        plot_id: str,
        *,
        expires_in_days: int = 30,
        label: str | None = None,
        allowed_origins: Sequence[str] = (),
    ) -> PlotEmbedCreated:
        """A tokenised read-only link (platform recipes only). The token is shown once."""
        body = PlotEmbedCreate(
            expires_in_days=expires_in_days, label=label, allowed_origins=list(allowed_origins)
        )
        return self._c.send(m3.create_plot_embed(plot_id, body))

    def embeds(self, plot_id: str, *, limit: int | None = None) -> Paginator[PlotEmbed]:
        return self._c.page_iter(lambda cur: m3.list_plot_embeds(plot_id, limit, cur))

    def revoke_embed(self, plot_id: str, embed_id: str) -> None:
        """Revoke an embed link (idempotent)."""
        self._c.send(m3.revoke_plot_embed(plot_id, embed_id))

    def embed(self, token: str) -> EmbedView:
        """The public view of an embed link (sent without credentials)."""
        return self._c.send(m3.get_embed(token))


# ======================================================================== trust lifecycle


class TenantTrustMixin(_Resource):
    """Verification, deletion and GDPR export on ``Client.tenants`` (v0.4)."""

    def verification(self, tenant_id: str) -> VerificationStatus:
        """E-mail and identity verification, and whether uploaded code is allowed."""
        return self._c.send(m3.get_tenant_verification(tenant_id))

    def request_deletion(
        self, tenant_id: str, *, confirm: str, reason: str | None = None
    ) -> TenantDeletion:
        """Schedule deletion of the whole tenant (``confirm`` must repeat the tenant id).
        The tenant is suspended for the grace period, then every store is purged."""
        if confirm != tenant_id:
            raise ValueError("confirm must repeat the tenant id exactly")
        body = TenantDeletionRequest(confirm=confirm, reason=reason)
        return self._c.send(m3.request_tenant_deletion(tenant_id, body))

    def deletion(self, tenant_id: str) -> TenantDeletion:
        """The deletion's state and progress per store."""
        return self._c.send(m3.get_tenant_deletion(tenant_id))

    def cancel_deletion(self, tenant_id: str) -> TenantDeletion:
        """Cancel a scheduled deletion (within the grace period)."""
        return self._c.send(m3.cancel_tenant_deletion(tenant_id))

    def export(self, tenant_id: str, *, include: Sequence[ExportPart] | None = None) -> GdprExport:
        """Start a GDPR export of everything the tenant stored."""
        body = GdprExportRequest() if include is None else GdprExportRequest(include=list(include))
        return self._c.send(m3.create_tenant_export(tenant_id, body))

    def export_status(self, tenant_id: str, export_id: str) -> GdprExport:
        return self._c.send(m3.get_tenant_export(tenant_id, export_id))

    def exports(self, tenant_id: str, *, limit: int | None = None) -> Paginator[GdprExport]:
        """The tenant's exports, newest first (``export_status`` gives a download link)."""
        return self._c.page_iter(lambda cur: m3.list_tenant_exports(tenant_id, limit, cur))

    def wait_export(
        self,
        tenant_id: str,
        export_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 2.0,
    ) -> GdprExport:
        """Poll until the export is ``ready`` (or ``failed``/``expired``)."""
        return _poll(
            self._c,
            lambda: self.export_status(tenant_id, export_id),
            export_done,
            what=f"export {export_id}",
            timeout=timeout,
            poll_interval=poll_interval,
        )

    def download_export(
        self,
        tenant_id: str,
        export_id: str,
        dest: str | os.PathLike[str],
        *,
        timeout: float | None = None,
    ) -> Path:
        """Wait for the export and download its archive to ``dest`` (written atomically;
        the size is checked against the announced one)."""
        export = self.wait_export(tenant_id, export_id, timeout=timeout)
        if export.state != "ready" or not export.download_url:
            raise ValueError(f"export {export_id} is {export.state}, nothing to download")
        return download_url(self._c, export.download_url, dest, expected_size=export.size_bytes)


class AdminTrustMixin(_Resource):
    """Operator side of the trust lifecycle on ``Client.admin`` (v0.4; audited)."""

    def set_verification(
        self,
        tenant_id: str,
        *,
        identity_verified: bool,
        method: VerificationMethod | str,
        reason: str,
    ) -> VerificationStatus:
        body = VerificationUpdate(
            identity_verified=identity_verified,
            method=VerificationMethod(method),
            reason=reason,
        )
        return self._c.send(m3.admin_put_tenant_verification(tenant_id, body))

    def signups(
        self,
        *,
        review: SignupReviewState | str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
    ) -> Paginator[SignupReview]:
        """Sign-ups, newest first (``review="flagged"`` is the Sybil review queue)."""
        return self._c.page_iter(
            lambda cur: m3.admin_list_signups(limit, cur, review=review, tenant_id=tenant_id)
        )

    def review_signup(
        self, signup_id: str, decision: Literal["approve", "reject"], reason: str
    ) -> SignupReview:
        """Approve or reject a sign-up (a rejection suspends its tenant)."""
        body = SignupReviewDecision(decision=decision, reason=reason)
        return self._c.send(m3.admin_review_signup(signup_id, body))

    def retry_deletion(self, tenant_id: str) -> TenantDeletion:
        """Restart a failed tenant purge."""
        return self._c.send(m3.admin_retry_tenant_deletion(tenant_id))


# ================================================================================ downloads


def download_url(
    client: Client,
    url: str,
    dest: str | os.PathLike[str],
    *,
    expected_size: int | None = None,
) -> Path:
    """Stream a signed link to ``dest`` through the storage client (no API credentials)."""
    sink = UrlSink(Path(dest), expected_size)
    try:
        with client._storage.stream("GET", url) as response:
            if response.status_code != 200:
                response.read()
                raise_for_response(response)
            for chunk in response.iter_bytes():
                sink.write(chunk)
        return sink.finish()
    except httpx.TransportError as exc:
        sink.abort()
        raise TransportError(f"download of {Path(dest).name} failed: {exc}") from exc
    except BaseException:
        sink.abort()
        raise
