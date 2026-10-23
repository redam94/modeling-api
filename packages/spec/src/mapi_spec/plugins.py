"""Uploaded plugins (v0.19): namespaces, version states, inspect and describe results, overlay
pins and the REST DTOs of ``/v1/plugins``.

A tenant admin or an attested editor uploads a pure-Python wheel. The control plane never
opens it: it stores the bytes, hashes them and starts two sandboxed runs.

* ``plugin_inspect`` reads the archive without importing anything and writes a
  :class:`PluginInspection` (facts, the SBOM and :class:`InspectProblem` rows);
* ``plugin_describe`` mounts the wheel as an overlay, exports its catalog entries and runs
  the kits; it writes a :class:`PluginDescribe`, which the control plane re-checks as data
  with :func:`check_describe` before the version reaches ``kit_passed``.

Kit results are a quality signal, not a security property: plugin code runs in the process
that runs the kits and can fake them. Every security decision uses inspect facts or the
re-checked describe JSON.

Namespaces are ``t_<tenant code>_<short name>`` (:func:`upload_namespace`); ids in them are
recognised with a pure string test (:func:`is_uploaded_id`). A run that uses uploaded code
carries :class:`OverlayPin` rows; the lineage of its model version keeps them
(:func:`lineage_pins`, :func:`lineage_has_uploaded_code`).

Pure Pydantic and stdlib (no archive parsing, no import of uploaded code): the control plane,
the runner and the CLI share it.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Collection, Iterator, Mapping
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from mapi_spec._dto import RequestModel, ResponseModel, Sha256, dto_examples
from mapi_spec._types import StrictModel
from mapi_spec.about import About, about_issues
from mapi_spec.catalog import (
    CatalogDiagnostic,
    ModelCatalogEntry,
    PlotCatalogEntry,
    StepCatalogEntry,
    TemplateCatalogEntry,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Problem, Severity, json_pointer
from mapi_spec.loader import canonical_value
from mapi_spec.namespaces import (
    UPLOAD_NAMESPACE_RE,
    is_uploaded_namespace,
    uploaded_namespace_of,
)
from mapi_spec.schema_safety import sanitize_schema
from mapi_spec.storage import PostPolicy
from mapi_spec.text_safety import has_unsafe_text, normalize_untrusted_text, skeleton
from mapi_spec.workflow_nodes import NodeImplementation, plugin_contract_issues

__all__ = [
    "DESCRIBE_MAX_BYTES",
    "INSPECTION_MAX_BYTES",
    "MOUNTABLE_STATES",
    "OVERLAYS_MAX",
    "PLUGIN_ENTRY_POINT_GROUPS",
    "PLUGIN_ID_PREFIX",
    "PLUGIN_ID_RE",
    "PLUGIN_SHORT_RE",
    "PLUGIN_TRANSITIONS",
    "PLUGIN_UPLOAD_ID_PREFIX",
    "PLUGIN_UPLOAD_ID_RE",
    "PLUGIN_VERSION_ID_PREFIX",
    "PLUGIN_VERSION_ID_RE",
    "PLUGIN_VERSION_STATES",
    "TENANT_CODE_RE",
    "UPLOAD_NAMESPACE_RE",
    "ArchiveLimits",
    "EntryPointDecl",
    "InspectCheck",
    "InspectProblem",
    "KitCheck",
    "KitStatus",
    "OverlayPin",
    "Plugin",
    "PluginClaim",
    "PluginDecision",
    "PluginDecisionRecord",
    "PluginDescribe",
    "PluginExports",
    "PluginInspection",
    "PluginKitReport",
    "PluginSbom",
    "PluginSettings",
    "PluginSettingsPatch",
    "PluginUploadComplete",
    "PluginUploadTicket",
    "PluginUploadTicketRequest",
    "PluginUsage",
    "PluginVersion",
    "PluginVersionState",
    "PluginVersionSummary",
    "PluginYank",
    "RequirementFact",
    "SbomFile",
    "VerifiedUploader",
    "YankReason",
    "check_describe",
    "check_transition",
    "describe_digest",
    "is_uploaded_id",
    "is_uploaded_namespace",
    "lineage_has_uploaded_code",
    "lineage_pins",
    "new_tenant_code",
    "parse_upload_namespace",
    "upload_dist_name",
    "upload_namespace",
    "uploaded_namespace_of",
]

# =========================================================================== namespaces and ids

TENANT_CODE_RE: Final = r"^[a-z2-7]{6}$"
"""The platform-assigned tenant code: 6 base32 characters (about 30 bits), never reused."""
PLUGIN_SHORT_RE: Final = r"^[a-z][a-z0-9]{1,15}$"
"""The short name a plugin claims (no ``_``, so the namespace parse is unambiguous)."""

_TENANT_CODE: Final = re.compile(TENANT_CODE_RE)
_SHORT: Final = re.compile(PLUGIN_SHORT_RE)
_UPLOAD_NAMESPACE: Final = re.compile(UPLOAD_NAMESPACE_RE)
_BASE32: Final = "abcdefghijklmnopqrstuvwxyz234567"

PLUGIN_ID_PREFIX: Final = "plg_"
PLUGIN_VERSION_ID_PREFIX: Final = "plv_"
PLUGIN_UPLOAD_ID_PREFIX: Final = "pup_"
PLUGIN_ID_RE: Final = r"^plg_[0-9a-z]{26}$"
PLUGIN_VERSION_ID_RE: Final = r"^plv_[0-9a-z]{26}$"
PLUGIN_UPLOAD_ID_RE: Final = r"^pup_[0-9a-z]{26}$"
"""Plugin, version and upload ids: the prefix plus a ULID (``mapi_spec._dto.new_id``)."""

PluginId = Annotated[str, Field(pattern=PLUGIN_ID_RE, examples=["plg_01jbz3q2x8m9v7k5h4g3f2e1d0"])]
PluginVersionId = Annotated[
    str, Field(pattern=PLUGIN_VERSION_ID_RE, examples=["plv_01jbz3q2x8m9v7k5h4g3f2e1d1"])
]
PluginUploadId = Annotated[
    str, Field(pattern=PLUGIN_UPLOAD_ID_RE, examples=["pup_01jbz3q2x8m9v7k5h4g3f2e1d2"])
]
UploadNamespace = Annotated[str, Field(pattern=UPLOAD_NAMESPACE_RE, examples=["t_k3vq7m_acme"])]
_UserId = Annotated[str, Field(min_length=1, max_length=64)]
_PEP440 = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[0-9][0-9A-Za-z.+!-]*$")]


def new_tenant_code() -> str:
    """A random tenant code (``[a-z2-7]{6}``, from :mod:`secrets`). The caller retries on a
    collision with the unique index."""
    return "".join(secrets.choice(_BASE32) for _ in range(6))


def upload_namespace(code: str, short: str) -> str:
    """``t_<code>_<short>`` (``upload_namespace("k3vq7m", "acme") == "t_k3vq7m_acme"``)."""
    if not _TENANT_CODE.match(code):
        raise ValueError(f"a tenant code matches {TENANT_CODE_RE}")
    if not _SHORT.match(short):
        raise ValueError(f"a plugin short name matches {PLUGIN_SHORT_RE}")
    return f"t_{code}_{short}"


def parse_upload_namespace(ns: str) -> tuple[str, str] | None:
    """``(tenant code, short name)`` of an uploaded namespace, ``None`` for anything else."""
    m = _UPLOAD_NAMESPACE.fullmatch(ns)
    return (m.group(1), m.group(2)) if m else None


def upload_dist_name(namespace: str) -> str:
    """The distribution name an upload must have: the PEP 503 form of its namespace
    (``t-k3vq7m-acme``)."""
    if not is_uploaded_namespace(namespace):
        raise ValueError(f"{namespace!r} is not an uploaded namespace")
    return namespace.replace("_", "-")


def is_uploaded_id(id_: str) -> bool:
    """True if ``id_`` (a catalog id or ref) is in an uploaded namespace.

    Forms: models, steps and plots ``t_k3vq7m_acme.store_mean``; workflow nodes
    ``<kind>.t_k3vq7m_acme_<name>``; workflow templates ``t_k3vq7m_acme_<name>``. Built-in and
    third-party ids are never uploaded ids (the ``t_`` prefix is reserved).
    """
    return uploaded_namespace_of(id_) is not None


# ============================================================================== version states

PluginVersionState = Literal[
    "uploaded",
    "inspecting",
    "inspect_failed",
    "describing",
    "kit_failed",
    "kit_passed",
    "awaiting_approval",
    "approved",
    "rejected",
    "yanked",
    "purged",
]
PLUGIN_VERSION_STATES: Final[tuple[PluginVersionState, ...]] = get_args(PluginVersionState)

PLUGIN_TRANSITIONS: Final[Mapping[PluginVersionState, frozenset[PluginVersionState]]] = {
    "uploaded": frozenset({"inspecting"}),
    "inspecting": frozenset({"inspect_failed", "describing"}),
    "inspect_failed": frozenset({"purged"}),
    "describing": frozenset({"kit_failed", "kit_passed"}),
    "kit_failed": frozenset({"purged"}),
    "kit_passed": frozenset({"awaiting_approval", "yanked"}),
    "awaiting_approval": frozenset({"approved", "rejected", "yanked"}),
    # D12: a rekit whose exports changed sends an approved version back to the reviewers
    "approved": frozenset({"yanked", "awaiting_approval"}),
    "rejected": frozenset({"purged"}),
    "yanked": frozenset({"purged"}),
    "purged": frozenset(),
}
"""Every allowed state change of a plugin version (D8 + the D12 rekit edge); the server's one
state writer refuses anything else with ``PLUGIN_STATE_CONFLICT``."""

MOUNTABLE_STATES: Final[frozenset[PluginVersionState]] = frozenset(
    {"kit_passed", "awaiting_approval", "approved"}
)
"""States whose overlay may be mounted (``approved`` for the tenant; the other two only for
the uploader's private use)."""

YankReason = Literal["withdrawn", "security", "superseded", "rekit_failed"]

PLUGIN_ENTRY_POINT_GROUPS: Final = (
    "mapi.models",
    "mapi.steps",
    "mapi.plots",
    "mapi.workflow_nodes",
    "mapi.workflow_templates",
)
"""The entry-point groups an upload may use (never ``mapi.governance_checks``)."""


def check_transition(current: PluginVersionState, new: PluginVersionState) -> None:
    """Raise ``MapiError(PLUGIN_STATE_CONFLICT)`` unless ``current -> new`` is allowed."""
    if new not in PLUGIN_TRANSITIONS[current]:
        raise MapiError(
            f"A plugin version in the state {current!r} cannot change to {new!r}.",
            code=ErrorCode.PLUGIN_STATE_CONFLICT,
        )


# =============================================================================== inspect


class ArchiveLimits(StrictModel):
    """Limits the inspect run applies to the wheel (the job carries the effective values;
    the defaults are the settings defaults)."""

    max_bytes: int = Field(20_971_520, ge=1)
    max_uncompressed: int = Field(104_857_600, ge=1)
    max_member: int = Field(20_971_520, ge=1)
    max_members: int = Field(2000, ge=1)
    max_ratio: int = Field(100, ge=1)
    max_depth: int = Field(16, ge=1)
    max_path: int = Field(255, ge=1)


InspectCheck = Literal[
    "archive_size",
    "member_count",
    "member_size",
    "ratio",
    "path_unsafe",
    "link_member",
    "special_member",
    "duplicate_name",
    "case_collision",
    "zip_structure",
    "encrypted",
    "compression_method",
    "nested_archive",
    "native_code",
    "bytecode",
    "pth_file",
    "site_hook",
    "top_level_name",
    "namespace_package",
    "dist_name",
    "dist_info",
    "wheel_tag",
    "data_dir",
    "entry_point_group",
    "entry_point_target",
    "requirement_unlocked",
    "requirement_url",
    "requires_python",
    "metadata_invalid",
    "text_unsafe",
    "sha_mismatch",
]
"""Stable names of the inspect checks (T19.2-T19.9, T19.14)."""


def _clean(max_len: int) -> Any:
    """A ``mode="before"`` validator body that normalizes untrusted text to ``max_len``
    characters (anything that is not a string is left for the type check to refuse)."""

    def check(v: Any) -> Any:
        return normalize_untrusted_text(v, max_len=max_len) if isinstance(v, str) else v

    return check


class InspectProblem(StrictModel):
    """One reason the inspect run refuses the wheel. ``message`` is platform-authored;
    ``path`` is the archive member concerned (untrusted, normalized)."""

    check: InspectCheck
    path: str | None = Field(None, max_length=255)
    message: str = Field(..., max_length=500)

    _path = field_validator("path", mode="before")(_clean(255))


class EntryPointDecl(StrictModel):
    """An entry point the wheel declares (read from ``entry_points.txt``, never loaded)."""

    group: str = Field(..., max_length=64)
    name: str = Field(..., max_length=128)
    value: str = Field(..., max_length=256)

    _group = field_validator("group", mode="before")(_clean(64))
    _name = field_validator("name", mode="before")(_clean(128))
    _value = field_validator("value", mode="before")(_clean(256))


class RequirementFact(StrictModel):
    """One ``Requires-Dist`` line and the version the base image has (``None``: missing)."""

    name: str = Field(..., max_length=128)
    specifier: str = Field("", max_length=256)
    marker: str | None = Field(None, max_length=256)
    locked_version: str | None = Field(None, max_length=64)

    _name = field_validator("name", mode="before")(_clean(128))
    _text = field_validator("specifier", "marker", mode="before")(_clean(256))
    _locked = field_validator("locked_version", mode="before")(_clean(64))


class SbomFile(StrictModel):
    """One archive member: its path, sha256 and uncompressed size."""

    path: str = Field(..., max_length=255)
    sha256: Sha256
    size: int = Field(..., ge=0)

    _path = field_validator("path", mode="before")(_clean(255))


class PluginSbom(StrictModel):
    """The software bill of materials of an upload: every file and every requirement."""

    format: Literal["mapi.sbom/v1"] = "mapi.sbom/v1"
    dist: str = Field(..., max_length=128)
    version: str = Field(..., max_length=64)
    files: list[SbomFile] = Field(default_factory=list, max_length=2000)
    requires: list[RequirementFact] = Field(default_factory=list, max_length=200)
    license: str | None = Field(None, max_length=200)
    base_image_digest: Sha256

    _dist = field_validator("dist", mode="before")(_clean(128))
    _version = field_validator("version", mode="before")(_clean(64))
    _license = field_validator("license", mode="before")(_clean(200))


class PluginInspection(StrictModel):
    """``plugin/inspection.json``: what the inspect run found (``ok`` = no problem)."""

    ok: bool
    archive_sha256: Sha256
    dist: str | None = Field(None, max_length=128)
    version: str | None = Field(None, max_length=64)
    wheel_tags: list[str] = Field(default_factory=list, max_length=8)
    top_level: list[str] = Field(default_factory=list, max_length=16)
    members: int = Field(0, ge=0)
    uncompressed_bytes: int = Field(0, ge=0)
    entry_points: list[EntryPointDecl] = Field(default_factory=list, max_length=500)
    requires_python: str | None = Field(None, max_length=128)
    sbom: PluginSbom | None = None
    problems: list[InspectProblem] = Field(default_factory=list, max_length=200)

    _text = field_validator("dist", "requires_python", mode="before")(_clean(128))
    _version = field_validator("version", mode="before")(_clean(64))

    @field_validator("wheel_tags", "top_level", mode="before")
    @classmethod
    def _names(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [
                normalize_untrusted_text(x, max_len=128) if isinstance(x, str) else x for x in v
            ]
        return v

    @model_validator(mode="after")
    def _ok(self) -> PluginInspection:
        if self.ok and self.problems:
            raise ValueError("an inspection with problems is not ok")
        return self


# ============================================================================== describe

KitStatus = Literal["pass", "fail", "skipped"]


class KitCheck(StrictModel):
    """One kit result. ``message`` comes from the sandbox (untrusted, normalized)."""

    entry_id: str | None = Field(None, max_length=128)
    kind: Literal["model", "step", "plot", "node", "template", "package"]
    check: str = Field(..., max_length=64)
    status: KitStatus
    message: str = Field("", max_length=500)

    _entry = field_validator("entry_id", mode="before")(_clean(128))
    _check = field_validator("check", mode="before")(_clean(64))
    _message = field_validator("message", mode="before")(_clean(500))


class PluginKitReport(StrictModel):
    """Every kit result of a describe run (``ok``: no ``fail``; ``skipped`` is allowed)."""

    ok: bool
    checks: list[KitCheck] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _ok(self) -> PluginKitReport:
        if self.ok and any(c.status == "fail" for c in self.checks):
            raise ValueError("a kit report with a failed check is not ok")
        return self


class PluginExports(StrictModel):
    """The catalog entries an upload exports (only its own distribution's)."""

    models: list[ModelCatalogEntry] = Field(default_factory=list, max_length=64)
    steps: list[StepCatalogEntry] = Field(default_factory=list, max_length=128)
    plots: list[PlotCatalogEntry] = Field(default_factory=list, max_length=128)
    workflow_nodes: list[NodeImplementation] = Field(default_factory=list, max_length=64)
    workflow_templates: list[TemplateCatalogEntry] = Field(default_factory=list, max_length=32)

    def counts(self) -> dict[Literal["model", "step", "plot", "node", "template"], int]:
        """Number of exports per kind (the ``exports`` of :class:`PluginVersionSummary`)."""
        return {
            "model": len(self.models),
            "step": len(self.steps),
            "plot": len(self.plots),
            "node": len(self.workflow_nodes),
            "template": len(self.workflow_templates),
        }

    def ids(self) -> list[str]:
        """Every exported id."""
        return [
            *(e.id for e in self.models),
            *(e.id for e in self.steps),
            *(e.id for e in self.plots),
            *(e.id for e in self.workflow_nodes),
            *(e.id for e in self.workflow_templates),
        ]


DESCRIBE_MAX_BYTES: Final = 2_097_152
"""Largest ``plugin/describe.json`` the control plane reads (checked before parsing)."""
INSPECTION_MAX_BYTES: Final = 1_048_576
"""Largest ``plugin/inspection.json`` the control plane reads (checked before parsing)."""
PLOT_EXAMPLES_MAX: Final = 64


class PluginDescribe(StrictModel):
    """``plugin/describe.json``: the exports, the recipe examples and the kit report of one
    version on one base image. The control plane treats all of it as data and re-checks it
    with :func:`check_describe`."""

    plugin_version_id: str = Field(..., pattern=PLUGIN_VERSION_ID_RE)
    overlay_sha256: Sha256
    base_image_digest: Sha256
    exports: PluginExports = Field(default_factory=PluginExports)
    plot_examples: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict,
        max_length=PLOT_EXAMPLES_MAX,
        description="Plot id -> its example as PlotSpec JSON (re-sanitized by the server)",
    )
    kit: PluginKitReport
    diagnostics: list[CatalogDiagnostic] = Field(default_factory=list, max_length=100)


def describe_digest(exports: PluginExports) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``exports`` (defaults left out, keys sorted).

    An approval names this digest: a rekit whose exports differ needs a new approval."""
    data = canonical_value(exports.model_dump(mode="json", by_alias=True, exclude_defaults=True))
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strings(value: Any, pointer: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield pointer, value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield from _strings(str(k), pointer)
            yield from _strings(v, _join(pointer, str(k)))
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            yield from _strings(v, _join(pointer, i))


def _join(base: str, part: str | int) -> str:
    return base + json_pointer(part)


def _schemas_in(value: Any, pointer: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every ``*_schema`` object inside a template document (defaults, params)."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            p = _join(pointer, str(k))
            if str(k).endswith("_schema") and isinstance(v, Mapping):
                yield p, v
            else:
                yield from _schemas_in(v, p)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _schemas_in(v, _join(pointer, i))


def check_describe(
    d: PluginDescribe, *, namespace: str, builtin_titles: Collection[str]
) -> list[Issue]:
    """Re-check a describe result as data (the control plane; empty list: nothing to fix).

    Error issues block ``kit_passed``. Each issue names its rule in ``context.check``:

    * ``size`` - the JSON is larger than :data:`DESCRIBE_MAX_BYTES`;
    * ``no_exports`` - the upload exports nothing;
    * ``namespace`` - an id is not in ``namespace`` (a built-in id, another upload's
      namespace, a node whose ``plugin`` is not ``namespace``, a template id that does not
      start with ``<namespace>_``);
    * ``duplicate`` - an id is exported twice;
    * ``node_contract`` - :func:`~mapi_spec.workflow_nodes.plugin_contract_issues`;
    * ``schema_unsafe`` - a settings, fit, params or template schema fails
      :mod:`mapi_spec.schema_safety` in ``refuse`` mode;
    * ``about_incomplete`` / ``reference_url`` - the About card is missing or incomplete, or a
      reference link breaks the upload URL rules (:func:`~mapi_spec.about.about_issues` with
      ``uploaded=True``);
    * ``text_unsafe`` - a string holds bidi controls, zero-width or control characters;
    * ``title_confusable`` - a title looks like one of ``builtin_titles``;
    * ``plot_example`` - an example names a plot the upload does not export;
    * ``provenance`` - an entry claims a ``provider`` other than ``upload:<plugin version
      id>`` or an ``overlay_hash`` other than ``overlay_sha256`` (the tenant view sets both).

    Uploaded text is never put in ``message``; it goes in ``untrusted_text``.
    """
    out: list[Issue] = []

    def bad(
        check: str,
        pointer: str,
        message: str,
        *,
        code: ErrorCode = ErrorCode.PLUGIN_REFUSED,
        untrusted: str | None = None,
    ) -> None:
        out.append(
            Issue(
                code=code,
                severity=Severity.ERROR,
                pointer=pointer,
                message=message,
                untrusted_text=None if untrusted is None else untrusted[:8000],
                context={"check": check},
            )
        )

    if not is_uploaded_namespace(namespace):
        raise ValueError(f"{namespace!r} is not an uploaded namespace")

    size = len(d.model_dump_json().encode("utf-8"))
    if size > DESCRIBE_MAX_BYTES:
        bad("size", "", f"The describe result is larger than {DESCRIBE_MAX_BYTES} bytes.")
    ex = d.exports
    if not ex.ids():
        bad(
            "no_exports",
            "/exports",
            "The package exports no model, step, recipe, node or template.",
        )

    def in_namespace(pointer: str, id_: str, what: str) -> None:
        if uploaded_namespace_of(id_) != namespace:
            bad(
                "namespace",
                pointer,
                f"The {what} id is not in the namespace {namespace!r} of this upload.",
                untrusted=id_,
            )

    def schema(pointer: str, value: Mapping[str, Any] | None) -> None:
        if value:
            _, issues = sanitize_schema(value, mode="refuse", where=pointer)
            for issue in issues:
                bad("schema_unsafe", issue.pointer, issue.message, code=ErrorCode.SCHEMA_UNSAFE)

    def about(pointer: str, card: About | None) -> None:
        for problem in about_issues(card, uploaded=True):
            check = "reference_url" if problem.startswith("reference") else "about_incomplete"
            bad(
                check,
                pointer,
                f"The About card is not complete: {problem}",
                code=ErrorCode.ABOUT_INCOMPLETE,
            )

    seen: dict[str, str] = {}

    def unique(pointer: str, key: str) -> None:
        if key in seen:
            bad(
                "duplicate",
                pointer,
                f"The id is exported twice (also at {seen[key]}).",
                untrusted=key,
            )
        else:
            seen[key] = pointer

    for i, m in enumerate(ex.models):
        p = json_pointer("exports", "models", i)
        in_namespace(p + "/manifest/id", m.id, "model")
        unique(p, f"model:{m.ref}")
        schema(p + "/manifest/settings_schema", m.manifest.settings_schema)
        schema(p + "/manifest/fit_schema", m.manifest.fit_schema)
        about(p + "/manifest/about", m.manifest.about)
    for i, s in enumerate(ex.steps):
        p = json_pointer("exports", "steps", i)
        in_namespace(p + "/id", s.id, "step")
        unique(p, f"step:{s.ref}")
        schema(p + "/settings_schema", s.settings_schema)
        about(p + "/about", s.about)
    for i, plot in enumerate(ex.plots):
        p = json_pointer("exports", "plots", i)
        in_namespace(p + "/id", plot.id, "recipe")
        unique(p, f"plot:{plot.ref}")
        schema(p + "/settings_schema", plot.settings_schema)
        about(p + "/about", plot.about)
    for i, node in enumerate(ex.workflow_nodes):
        p = json_pointer("exports", "workflow_nodes", i)
        in_namespace(p + "/id", node.id, "workflow node")
        unique(p, f"node:{node.id}")
        if node.plugin != namespace:
            bad(
                "namespace",
                p + "/plugin",
                f"A node of this upload must name the namespace {namespace!r} in 'plugin'.",
                untrusted=node.plugin,
            )
        for issue in plugin_contract_issues(node):
            # the namespace rule for uploads is the one above (the generic rule refuses
            # every t_ namespace); everything else applies unchanged
            if issue.pointer == "/plugin":
                continue
            bad(
                "about_incomplete" if issue.code is ErrorCode.ABOUT_INCOMPLETE else "node_contract",
                p + issue.pointer,
                "The workflow node contract is not valid.",
                code=issue.code,
                untrusted=issue.message,
            )
    for i, t in enumerate(ex.workflow_templates):
        p = json_pointer("exports", "workflow_templates", i)
        if not t.id.startswith(f"{namespace}_"):
            bad(
                "namespace",
                p + "/id",
                f"A template id of this upload must start with '{namespace}_'.",
                untrusted=t.id,
            )
        else:
            in_namespace(p + "/id", t.id, "workflow template")
        unique(p, f"template:{t.id}")
        about(p + "/about", t.about)
        for where, value in _schemas_in(t.document, p + "/document"):
            schema(where, value)

    provider = f"upload:{d.plugin_version_id}"
    entries: list[tuple[str, str | None, str | None]] = []
    for group, items in (
        ("models", ex.models),
        ("steps", ex.steps),
        ("plots", ex.plots),
        ("workflow_templates", ex.workflow_templates),
    ):
        for i, entry in enumerate(items):
            assert isinstance(
                entry,
                ModelCatalogEntry | StepCatalogEntry | PlotCatalogEntry | TemplateCatalogEntry,
            )
            entries.append((json_pointer("exports", group, i), entry.provider, entry.overlay_hash))
    for pointer, claimed_provider, claimed_hash in entries:
        if claimed_provider not in (None, provider) or claimed_hash not in (
            None,
            d.overlay_sha256,
        ):
            bad(
                "provenance",
                pointer,
                f"An entry of this upload may name only {provider!r} as its provider and "
                "this upload's sha256 as its overlay hash.",
                untrusted=claimed_provider,
            )

    for plot_id in d.plot_examples:
        if plot_id not in {e.id for e in ex.plots}:
            bad(
                "plot_example",
                json_pointer("plot_examples", plot_id),
                "An example names a recipe that the upload does not export.",
                untrusted=plot_id,
            )

    exports_json = ex.model_dump(mode="json", exclude_defaults=True)
    for pointer, text in _strings(exports_json, "/exports"):
        if has_unsafe_text(text):
            bad(
                "text_unsafe",
                pointer,
                "The text holds hidden, bidirectional or control characters. Remove them.",
                untrusted=normalize_untrusted_text(text, max_len=500),
            )
    for i, diag in enumerate(d.diagnostics):
        for pointer, text in _strings(diag.model_dump(mode="json"), json_pointer("diagnostics", i)):
            if has_unsafe_text(text):
                bad(
                    "text_unsafe",
                    pointer,
                    "The text holds hidden, bidirectional or control characters.",
                    untrusted=normalize_untrusted_text(text, max_len=500),
                )

    builtin = {k for k in (skeleton(t) for t in builtin_titles) if k}
    for pointer, title in _titles(ex):
        key = skeleton(title)
        if key and key in builtin:
            bad(
                "title_confusable",
                pointer,
                "The title looks like the title of a built-in item. Use a different title.",
                untrusted=normalize_untrusted_text(title, max_len=200),
            )
    return out


def _titles(ex: PluginExports) -> Iterator[tuple[str, str]]:
    for i, m in enumerate(ex.models):
        if m.manifest.about is not None:
            yield (
                json_pointer("exports", "models", i, "manifest", "about", "title"),
                m.manifest.about.title,
            )
    for i, s in enumerate(ex.steps):
        if s.about is not None:
            yield json_pointer("exports", "steps", i, "about", "title"), s.about.title
    for i, plot in enumerate(ex.plots):
        yield json_pointer("exports", "plots", i, "title"), plot.title
        if plot.about is not None and plot.about.title != plot.title:
            yield json_pointer("exports", "plots", i, "about", "title"), plot.about.title
    for i, node in enumerate(ex.workflow_nodes):
        yield json_pointer("exports", "workflow_nodes", i, "about", "title"), node.about.title
    for i, t in enumerate(ex.workflow_templates):
        yield json_pointer("exports", "workflow_templates", i, "about", "title"), t.about.title


# =============================================================================== overlay pins

OVERLAYS_MAX: Final = 8
"""Most overlays one run may mount."""


class OverlayPin(StrictModel):
    """One uploaded plugin version a run uses: which version, its namespace and the sha256 of
    the wheel the control plane stored. Set by the control plane, never by a caller."""

    plugin_version_id: str = Field(..., pattern=PLUGIN_VERSION_ID_RE)
    namespace: str = Field(..., pattern=UPLOAD_NAMESPACE_RE)
    sha256: Sha256

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "plugin_version_id": "plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
                    "namespace": "t_k3vq7m_acme",
                    "sha256": "sha256:" + "ab" * 32,
                }
            ]
        }
    )


def lineage_pins(lineage: Mapping[str, Any] | None) -> list[OverlayPin]:
    """The overlay pins a model version's lineage records (``lineage["overlays"]``).

    The legacy singular ``overlay_hash`` cannot be turned into a pin (it names no version);
    it gives ``[]`` here and :func:`lineage_has_uploaded_code` still reports it. A malformed
    ``overlays`` value raises ``MapiError(INTEGRITY_FAILURE)`` (fail closed).
    """
    if not lineage:
        return []
    raw = lineage.get("overlays") or []
    if not isinstance(raw, list):
        raise MapiError(
            "The lineage of this model version records uploaded code in a form that is not valid.",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    try:
        return [OverlayPin.model_validate(item) for item in raw]
    except ValidationError:
        raise MapiError(
            "The lineage of this model version records uploaded code in a form that is not valid.",
            code=ErrorCode.INTEGRITY_FAILURE,
        ) from None


def lineage_has_uploaded_code(lineage: Mapping[str, Any] | None) -> bool:
    """True if a lineage records uploaded code: a non-empty ``overlays`` (whatever its form)
    or the legacy ``overlay_hash`` key set."""
    if not lineage:
        return False
    return bool(lineage.get("overlays")) or bool(lineage.get("overlay_hash"))


# ======================================================================================== DTOs

_TS: Final = "2026-10-22T09:30:00Z"
_SHA_A: Final = "sha256:" + "ab" * 32
_SHA_D: Final = "sha256:" + "cd" * 32
_SHA_I: Final = "sha256:" + "ef" * 32
_PLG: Final = "plg_01jbz3q2x8m9v7k5h4g3f2e1d0"
_PLV: Final = "plv_01jbz3q2x8m9v7k5h4g3f2e1d1"
_PUP: Final = "pup_01jbz3q2x8m9v7k5h4g3f2e1d2"
_USR: Final = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
_ADM: Final = "usr_01jaf3a9b8c7d6e5f4g3h2j1k9"

ExportKind = Literal["model", "step", "plot", "node", "template"]

_SUMMARY_EXAMPLE: Final[dict[str, Any]] = {
    "id": _PLV,
    "plugin_id": _PLG,
    "version": "0.1.0",
    "state": "kit_passed",
    "scope": "private",
    "uploaded_by": _USR,
    "uploaded_at": _TS,
    "overlay_sha256": _SHA_A,
    "describe_digest": _SHA_D,
    "exports": {"model": 1, "step": 1, "plot": 1, "node": 1, "template": 1},
}


class PluginVersionSummary(ResponseModel):
    """One uploaded version of a plugin (lists)."""

    id: PluginVersionId
    plugin_id: PluginId
    version: _PEP440
    state: PluginVersionState
    scope: Literal["private", "tenant", "none"] = Field(
        ...,
        description="Who may use it now: only the uploader (private), the whole tenant, or nobody",
    )
    uploaded_by: _UserId
    uploaded_at: AwareDatetime
    overlay_sha256: Sha256 | None = None
    describe_digest: Sha256 | None = None
    exports: dict[ExportKind, int] = Field(default_factory=dict)

    model_config = dto_examples(_SUMMARY_EXAMPLE)


class PluginDecisionRecord(ResponseModel):
    """One approval or rejection of a version, with the digests the reviewer saw."""

    decision: Literal["approve", "reject"]
    approver_id: _UserId
    approver_name: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=2000, description="Untrusted text")
    overlay_sha256: Sha256
    describe_digest: Sha256
    base_image_digest: Sha256
    decided_at: AwareDatetime

    model_config = dto_examples(
        {
            "decision": "approve",
            "approver_id": _ADM,
            "approver_name": "Ada Admin",
            "overlay_sha256": _SHA_A,
            "describe_digest": _SHA_D,
            "base_image_digest": _SHA_I,
            "decided_at": _TS,
        }
    )


class PluginVersion(PluginVersionSummary):
    """One version with everything the version page and the reviewer panel show."""

    inspection: PluginInspection | None = None
    kit: PluginKitReport | None = None
    exports_detail: PluginExports | None = None
    issues: list[Issue] = Field(default_factory=list)
    base_image_digest: Sha256 | None = None
    approvals: list[PluginDecisionRecord] = Field(default_factory=list)
    problem: Problem | None = None
    yank_reason: YankReason | None = None
    yank_note: str | None = Field(None, max_length=2000)
    purge_after: AwareDatetime | None = None
    urls: list[str] = Field(
        default_factory=list, description="Every About reference URL, for the reviewer"
    )

    model_config = dto_examples(
        {
            **_SUMMARY_EXAMPLE,
            "inspection": {
                "ok": True,
                "archive_sha256": _SHA_A,
                "dist": "t-k3vq7m-acme",
                "version": "0.1.0",
                "wheel_tags": ["py3-none-any"],
                "top_level": ["t_k3vq7m_acme"],
                "members": 11,
                "uncompressed_bytes": 40960,
                "entry_points": [
                    {
                        "group": "mapi.steps",
                        "name": "acme",
                        "value": "t_k3vq7m_acme.steps:STEPS",
                    }
                ],
                "requires_python": ">=3.12",
                "problems": [],
            },
            "kit": {
                "ok": True,
                "checks": [
                    {
                        "entry_id": "t_k3vq7m_acme.winsorize",
                        "kind": "step",
                        "check": "check_step",
                        "status": "pass",
                        "message": "",
                    }
                ],
            },
            "base_image_digest": _SHA_I,
            "urls": ["https://example.org/acme"],
        }
    )


class Plugin(ResponseModel):
    """A claimed plugin name of a tenant and its latest version."""

    id: PluginId
    namespace: UploadNamespace
    short_name: str = Field(..., pattern=PLUGIN_SHORT_RE)
    title: str = Field(..., max_length=120)
    created_by: _UserId
    created_at: AwareDatetime
    updated_at: AwareDatetime
    latest_version: PluginVersionSummary | None = None
    versions: int = Field(0, ge=0)

    model_config = dto_examples(
        {
            "id": _PLG,
            "namespace": "t_k3vq7m_acme",
            "short_name": "acme",
            "title": "Acme store tools",
            "created_by": _USR,
            "created_at": _TS,
            "updated_at": _TS,
            "latest_version": _SUMMARY_EXAMPLE,
            "versions": 1,
        }
    )


def _untrusted_title(v: str) -> str:
    text = normalize_untrusted_text(v, max_len=120)
    if not text:
        raise ValueError("the title must not be empty")
    return text


def _untrusted_note(v: str | None) -> str | None:
    if v is None:
        return None
    return normalize_untrusted_text(v, max_len=2000) or None


class PluginClaim(RequestModel):
    """Claim a short name: the namespace becomes ``t_<tenant code>_<short_name>``."""

    short_name: str = Field(..., pattern=PLUGIN_SHORT_RE)
    title: str = Field(..., min_length=1, max_length=120)

    model_config = dto_examples({"short_name": "acme", "title": "Acme store tools"})

    _title = field_validator("title")(_untrusted_title)


class PluginUploadTicketRequest(RequestModel):
    """Ask for an upload ticket. ``size`` above the deployment's cap is refused by the route
    with ``PAYLOAD_TOO_LARGE``."""

    filename: str = Field(..., max_length=255, pattern=r"^[A-Za-z0-9._-]+\.whl$")
    size: int = Field(..., ge=1, le=2**40)

    model_config = dto_examples({"filename": "t_k3vq7m_acme-0.1.0-py3-none-any.whl", "size": 12960})


class PluginUploadTicket(ResponseModel):
    """Where to POST the wheel: a POST policy for exactly one quarantine key."""

    upload_id: PluginUploadId
    post: PostPolicy
    expires_at: AwareDatetime
    max_bytes: int = Field(..., ge=1)

    model_config = dto_examples(
        {
            "upload_id": _PUP,
            "post": {
                "method": "POST",
                "url": "http://127.0.0.1:4443/mapi-local-quarantine",
                "prefix": f"t/default/plugins/{_PUP}/",
                "fields": {
                    "key": f"t/default/plugins/{_PUP}/plugin.whl",
                    "policy": "eyJjb25kaXRpb25zIjpbXX0=",
                    "x-goog-signature": "abc123",
                },
                "content_length_range": [1, 20971520],
                "expires_at": "2026-10-22T09:45:00Z",
                "create_only": True,
            },
            "expires_at": "2026-10-22T09:45:00Z",
            "max_bytes": 20971520,
        }
    )


class PluginUploadComplete(RequestModel):
    """The wheel is uploaded: check it. ``project`` pays for the pipeline runs."""

    project: Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")]
    request_approval: bool = False

    model_config = dto_examples({"project": "growth", "request_approval": False})


class PluginDecision(RequestModel):
    """Approve or reject a version. The digests must be the ones the page showed (else
    ``PLUGIN_STATE_CONFLICT``); a rejection needs a ``note``."""

    overlay_sha256: Sha256
    describe_digest: Sha256
    base_image_digest: Sha256
    note: str | None = Field(None, max_length=2000)

    model_config = dto_examples(
        {"overlay_sha256": _SHA_A, "describe_digest": _SHA_D, "base_image_digest": _SHA_I},
        {
            "overlay_sha256": _SHA_A,
            "describe_digest": _SHA_D,
            "base_image_digest": _SHA_I,
            "note": "The recipe sends data to an external host.",
        },
    )

    _note = field_validator("note")(_untrusted_note)


class PluginYank(RequestModel):
    """Withdraw a version (``security`` also cancels queued and running runs that use it)."""

    reason: Literal["withdrawn", "security", "superseded"]
    note: str | None = Field(None, max_length=2000)

    model_config = dto_examples({"reason": "superseded", "note": "Use 0.2.0."})

    _note = field_validator("note")(_untrusted_note)


class PluginUsage(ResponseModel):
    """What uses a version (shown before a yank)."""

    runs_30d: int = Field(0, ge=0)
    model_versions: list[str] = Field(default_factory=list, max_length=50)
    endpoints: list[str] = Field(default_factory=list, max_length=50)
    dashboards: list[str] = Field(default_factory=list, max_length=50)
    truncated: bool = False

    model_config = dto_examples(
        {
            "runs_30d": 4,
            "model_versions": ["mv_01jbz3q2x8m9v7k5h4g3f2e1d3"],
            "endpoints": [],
            "dashboards": [],
            "truncated": False,
        }
    )


class PluginSettings(ResponseModel):
    """The tenant's uploaded-code settings and whether uploads are allowed (and why not)."""

    tenant_code: str | None = Field(None, pattern=TENANT_CODE_RE)
    private_use: bool = True
    uploads_allowed: bool = False
    reason: str = Field("", max_length=500)

    model_config = dto_examples(
        {"tenant_code": "k3vq7m", "private_use": True, "uploads_allowed": True, "reason": ""}
    )


class PluginSettingsPatch(RequestModel):
    """Turn private use of kit-passed uploads on or off for the tenant."""

    private_use: bool

    model_config = dto_examples({"private_use": False})


class VerifiedUploader(ResponseModel):
    """An editor a tenant admin attested (identity-verified for uploads)."""

    user_id: _UserId
    display_name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=320)
    method: Literal["admin_attested", "provider"]
    verified_by: _UserId
    verified_at: AwareDatetime

    model_config = dto_examples(
        {
            "user_id": _USR,
            "display_name": "Eve Editor",
            "email": "eve@example.com",
            "method": "admin_attested",
            "verified_by": _ADM,
            "verified_at": _TS,
        }
    )
