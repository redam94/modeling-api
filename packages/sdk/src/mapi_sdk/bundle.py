"""Fitted-model bundles: one ``bundle.zarr.zip`` DataTree plus JSON sidecars (plan §8.6).

Layout of ``bundle.zarr.zip``::

    /                       attrs: format, bundle_id, role
    /spec                   attrs: spec (canonical JSON), resolved (ResolvedSpec JSON)
    /schema/raw             attrs: json (dataset-named raw DataSchema)
    /schema/model           attrs: json (model input schema, effective policies)
    /binding                attrs: json
    /preprocess/<step_id>   attrs: state (JSON); children: arrays, context
    /model                  attrs: manifest, settings, fit_config, learned (JSON)
    /model/<group>          ArviZ-named groups (posterior, sample_stats, log_likelihood ...)
    /terminal_state         plugin-owned state for sequential models
    /native                 uint8 arrays of safe native artifacts (skops, xgb json, ...)
    /derived/<name>         derived quantities cached at fit
    /fit_coords/<stage>     coordinates of the fitted data at every stage
    /side_tables/<name>     pinned side tables used at fit
    /residuals              model-space residuals (point models, smearing)
    /thin                   the bundle-wide draw thinning index

Sidecars: ``manifest.json`` (content hashes of every file: size, CRC32C and MD5 in the GCS
base64 form, SHA-256), ``diagnostics.json``, ``plots.json`` and ``metrics.json``. An
``evaluate_then_refit`` bundle keeps its train-only evaluation bundle under
``evaluation/``. Pickle is never written and is rejected on read.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

import google_crc32c
import numpy as np
import xarray as xr
from mapi_spec.capabilities import Diagnostic, ModelManifest
from mapi_spec.loader import canonical_json
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ModelSpec
from mapi_spec.validate import ResolvedSpec
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mapi_sdk.binding import BindingMap
from mapi_sdk.errors import BundleError, MapiError, WireFormatError
from mapi_sdk.models import Fitted
from mapi_sdk.pipeline import Pipeline
from mapi_sdk.registry import Registry
from mapi_sdk.steps import StepState
from mapi_sdk.wire import DEFAULT_LIMITS, WireLimits, read_datatree_zip, write_datatree_zip

__all__ = [
    "BUNDLE_FILE",
    "BUNDLE_FORMAT",
    "Bundle",
    "BundleManifest",
    "FileDigest",
    "MetricsReport",
    "assert_no_pickle",
    "file_digest",
]

BUNDLE_FORMAT = "mapi-bundle/v1"
BUNDLE_FILE = "bundle.zarr.zip"
SIDECARS = ("manifest.json", "diagnostics.json", "plots.json", "metrics.json")
_PICKLE_SUFFIXES = (".pkl", ".pickle", ".joblib", ".cloudpickle", ".pt", ".pth", ".npy_pickle")


class FileDigest(BaseModel):
    """Content hashes of one file, in the forms GCS reports (CRC32C/MD5 base64)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    size: int
    crc32c: str
    md5: str
    sha256: str


def file_digest(data: bytes) -> FileDigest:
    """Size, CRC32C (base64, big-endian), MD5 (base64) and SHA-256 (hex) of ``data``."""
    return FileDigest(
        size=len(data),
        crc32c=base64.b64encode(_crc32c(data)).decode(),
        md5=base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode(),
        sha256=hashlib.sha256(data).hexdigest(),
    )


class MetricsReport(BaseModel):
    """Holdout metrics computed through the score path, plus skipped metrics and why."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, float | None] = Field(default_factory=dict)
    skipped: dict[str, str] = Field(default_factory=dict)
    n_holdout: int = 0
    split: dict[str, JsonValue] | None = None
    evaluation_bundle: str | None = None
    output_space: str | None = None


class BundleManifest(BaseModel):
    """Bundle identity, provenance and the content hashes of its files."""

    model_config = ConfigDict(extra="forbid")

    format: str = BUNDLE_FORMAT
    bundle_id: str
    role: Literal["production", "evaluation"] = "production"
    created_at: str
    spec_hash: str
    data_hash: str | None = None
    plugin: dict[str, JsonValue] = Field(default_factory=dict)
    steps: list[dict[str, JsonValue]] = Field(default_factory=list)
    fit: dict[str, JsonValue] = Field(default_factory=dict)
    lineage: dict[str, JsonValue] = Field(default_factory=dict)
    lib_versions: dict[str, str] = Field(default_factory=dict)
    files: dict[str, FileDigest] = Field(default_factory=dict)


def lib_versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for dist in ("numpy", "xarray", "zarr", "pandas", "mapi-spec", "mapi-sdk"):
        try:
            out[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:  # pragma: no cover - source checkout
            out[dist] = "unknown"
    return out


def assert_no_pickle(native: Mapping[str, bytes]) -> None:
    """Reject pickle-like native artifacts (by name or by content)."""
    for name, data in native.items():
        lower = name.lower()
        if lower.endswith(_PICKLE_SUFFIXES):
            raise BundleError(f"native artifact {name!r} looks like a pickle; use a safe format")
        if data[:1] == b"\x80" and len(data) > 1 and data[1] in range(2, 6):
            raise BundleError(f"native artifact {name!r} contains pickle data")
        if data[:2] == b"PK" and (b"data.pkl" in data[:4096] or b".pkl" in data[-4096:]):
            raise BundleError(f"native artifact {name!r} is an archive containing pickle data")


def _subtree(tree: xr.DataTree, path: str) -> xr.DataTree:
    try:
        sub = tree[path]
    except KeyError:
        raise BundleError(f"bundle has no {path!r} group") from None
    if not isinstance(sub, xr.DataTree):
        raise BundleError(f"bundle entry {path!r} is not a group")
    return sub


_CHECKSUM: Any = google_crc32c.Checksum  # untyped third-party API


def _crc32c(data: bytes) -> bytes:
    digest: bytes = _CHECKSUM(data).digest()
    return digest


def _attrs_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load_json(attrs: Mapping[str, Any], key: str, where: str) -> Any:
    raw = attrs.get(key)
    if not isinstance(raw, str):
        raise BundleError(f"bundle group {where!r} has no {key!r} attribute")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BundleError(f"bundle group {where!r} has invalid JSON in {key!r}") from exc


@dataclass(slots=True)
class Bundle:
    """A fitted model: spec, schemas, preprocessing states, model groups and sidecars."""

    spec: ModelSpec
    resolved: ResolvedSpec
    raw_schema: DataSchema
    states: dict[str, StepState]
    fit_coords: dict[str, xr.Dataset]
    model: Fitted
    manifest: BundleManifest
    thin: np.ndarray | None = None
    side_tables: dict[str, xr.Dataset] = field(default_factory=dict)
    residuals: dict[str, xr.DataArray] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    metrics: MetricsReport = field(default_factory=MetricsReport)
    plots: list[dict[str, Any]] = field(default_factory=list)
    evaluation: Bundle | None = None

    # ------------------------------------------------------------------ views
    @property
    def binding(self) -> BindingMap:
        return BindingMap(self.resolved.binding)

    @property
    def bundle_id(self) -> str:
        return self.manifest.bundle_id

    @property
    def model_stage(self) -> str:
        return self.resolved.model_stage

    @property
    def model_schema(self) -> DataSchema | None:
        return self.resolved.model.input_schema

    def pipeline(self, registry: Registry) -> Pipeline:
        return Pipeline.from_spec(self.spec, registry, self.resolved)

    # ------------------------------------------------------------------ tree
    def to_tree(self) -> xr.DataTree:
        """The ``bundle.zarr.zip`` DataTree (see the module docstring for the layout)."""
        nodes: dict[str, xr.Dataset] = {
            "/": xr.Dataset(
                attrs={
                    "format": BUNDLE_FORMAT,
                    "bundle_id": self.bundle_id,
                    "role": self.manifest.role,
                }
            ),
            "/spec": xr.Dataset(
                attrs={
                    "spec": canonical_json(self.spec),
                    "resolved": self.resolved.model_dump_json(by_alias=True),
                }
            ),
            "/schema/raw": xr.Dataset(attrs={"json": self.raw_schema.model_dump_json()}),
            "/binding": xr.Dataset(attrs={"json": self.resolved.binding.model_dump_json()}),
        }
        if self.model_schema is not None:
            nodes["/schema/model"] = xr.Dataset(attrs={"json": self.model_schema.model_dump_json()})
        for sid, st in self.states.items():
            base = f"/preprocess/{sid}"
            nodes[base] = xr.Dataset(attrs={"state": _attrs_json(st.json_payload())})
            if st.arrays is not None:
                nodes[f"{base}/arrays"] = st.arrays
            if st.context is not None:
                nodes[f"{base}/context"] = st.context
        m = self.model
        assert_no_pickle(m.native)
        nodes["/model"] = xr.Dataset(
            attrs={
                "manifest": m.manifest.model_dump_json(by_alias=True),
                "settings": _attrs_json(m.settings),
                "fit_config": m.fit_config.model_dump_json(by_alias=True),
                "learned": _attrs_json(m.learned),
            }
        )
        for name, grp in m.groups.items():
            nodes[f"/model/{name}"] = grp
        if m.terminal_state is not None:
            nodes["/terminal_state"] = m.terminal_state
        if m.native:
            arrays = {
                f"file_{i}": xr.DataArray(
                    np.frombuffer(data, dtype=np.uint8), dims=[f"bytes_{i}"], attrs={"name": name}
                )
                for i, (name, data) in enumerate(sorted(m.native.items()))
            }
            nodes["/native"] = xr.Dataset(arrays)
        for name, d in m.derived.items():
            nodes[f"/derived/{name}"] = d
        for stage, coords in self.fit_coords.items():
            nodes[f"/fit_coords/{stage}"] = coords
        for name, table in self.side_tables.items():
            nodes[f"/side_tables/{name}"] = table
        if self.residuals:
            nodes["/residuals"] = xr.Dataset({k: v.rename(None) for k, v in self.residuals.items()})
        if self.thin is not None:
            nodes["/thin"] = xr.Dataset({"index": (("sample", "pair"), self.thin)})
        return xr.DataTree.from_dict(nodes)

    # ------------------------------------------------------------------ write/read
    def write(self, path: str | Path) -> BundleManifest:
        """Write the bundle directory; returns the manifest with file hashes filled in."""
        root = Path(path)
        root.mkdir(parents=True, exist_ok=True)
        data = write_datatree_zip(self.to_tree())
        assert data is not None
        files: dict[str, bytes] = {
            BUNDLE_FILE: data,
            "diagnostics.json": json.dumps(
                [d.model_dump(mode="json") for d in self.diagnostics], indent=2
            ).encode(),
            "plots.json": json.dumps(self.plots, indent=2, allow_nan=False).encode(),
            "metrics.json": self.metrics.model_dump_json(indent=2).encode(),
        }
        for name, content in files.items():
            (root / name).write_bytes(content)
        digests = {name: file_digest(content) for name, content in files.items()}
        if self.evaluation is not None:
            ev = self.evaluation.write(root / "evaluation")
            ev_bytes = (root / "evaluation" / "manifest.json").read_bytes()
            digests["evaluation/manifest.json"] = file_digest(ev_bytes)
            self.manifest.lineage["evaluation_bundle"] = ev.bundle_id
        self.manifest.files = digests
        (root / "manifest.json").write_text(self.manifest.model_dump_json(indent=2))
        return self.manifest

    @classmethod
    def read(
        cls,
        path: str | Path,
        registry: Registry,
        *,
        verify: bool = True,
        limits: WireLimits = DEFAULT_LIMITS,
    ) -> Bundle:
        """Read a bundle directory (verifying file hashes against ``manifest.json``)."""
        root = Path(path)
        try:
            manifest = BundleManifest.model_validate_json((root / "manifest.json").read_bytes())
        except (OSError, ValidationError) as exc:
            raise BundleError(f"bundle manifest is missing or invalid: {exc}") from None
        if manifest.format != BUNDLE_FORMAT:
            raise BundleError(f"unsupported bundle format {manifest.format!r}")
        contents: dict[str, bytes] = {}
        for name in (BUNDLE_FILE, *SIDECARS[1:]):
            try:
                contents[name] = (root / name).read_bytes()
            except OSError as exc:
                raise BundleError(f"bundle file {name!r} is missing") from exc
            if verify:
                want = manifest.files.get(name)
                if want is None or file_digest(contents[name]) != want:
                    raise BundleError(f"bundle file {name!r} does not match its manifest hash")
        try:
            tree = read_datatree_zip(contents[BUNDLE_FILE], limits=limits)
        except WireFormatError as exc:
            raise BundleError(f"bundle archive is unreadable: {exc.detail}") from None
        bundle = cls.from_tree(tree, registry, manifest=manifest)
        try:
            bundle.diagnostics = [
                Diagnostic.model_validate(d) for d in json.loads(contents["diagnostics.json"])
            ]
            bundle.plots = list(json.loads(contents["plots.json"]))
            bundle.metrics = MetricsReport.model_validate_json(contents["metrics.json"])
        except (ValidationError, json.JSONDecodeError, TypeError) as exc:
            raise BundleError(f"bundle sidecars are invalid: {exc}") from None
        if (root / "evaluation" / "manifest.json").exists():
            bundle.evaluation = cls.read(
                root / "evaluation", registry, verify=verify, limits=limits
            )
        return bundle

    @classmethod
    def from_tree(
        cls, tree: xr.DataTree, registry: Registry, *, manifest: BundleManifest
    ) -> Bundle:
        """Rebuild a :class:`Bundle` from its DataTree (typed states need the registry)."""
        if tree.attrs.get("format") != BUNDLE_FORMAT:
            raise BundleError("not a mapi bundle (missing format attribute)")

        def node(path: str) -> xr.Dataset:
            return _subtree(tree, path).to_dataset(inherit=False)

        def children(path: str) -> dict[str, xr.Dataset]:
            if path.strip("/") not in tree:
                return {}
            sub = _subtree(tree, path)
            return {name: child.to_dataset(inherit=False) for name, child in sub.children.items()}

        try:
            spec = ModelSpec.model_validate_json(node("/spec").attrs["spec"])
            resolved = ResolvedSpec.model_validate_json(node("/spec").attrs["resolved"])
            raw_schema = DataSchema.model_validate_json(node("/schema/raw").attrs["json"])
            mattrs = node("/model").attrs
            mman = ModelManifest.model_validate_json(mattrs["manifest"])
            fit_cfg = FitConfig.model_validate_json(mattrs["fit_config"])
        except (KeyError, ValidationError) as exc:
            raise BundleError(f"bundle metadata is invalid: {exc}") from None
        settings = _load_json(mattrs, "settings", "/model")
        learned = _load_json(mattrs, "learned", "/model")

        states: dict[str, StepState] = {}
        for sid in [s.id for s in spec.pipeline]:
            snode = _subtree(tree, f"/preprocess/{sid}")
            payload = _load_json({str(k): v for k, v in snode.attrs.items()}, "state", sid)
            ref = f"{payload.get('step_type')}@{payload.get('step_version')}"
            try:
                step_cls = registry.step(ref)
            except MapiError:
                raise BundleError(f"step {ref!r} used by this bundle is not installed") from None
            state_cls = step_cls.State or StepState
            arrays = (
                _subtree(snode, "arrays").to_dataset(inherit=False) if "arrays" in snode else None
            )
            context = (
                _subtree(snode, "context").to_dataset(inherit=False) if "context" in snode else None
            )
            states[sid] = state_cls.from_payload(payload, arrays, context)

        groups = children("/model")
        native: dict[str, bytes] = {}
        if "native" in tree:
            for var in node("/native").data_vars.values():
                native[str(var.attrs["name"])] = np.asarray(var.values, dtype=np.uint8).tobytes()
        assert_no_pickle(native)
        model_schema = resolved.model.input_schema
        fit_coords = children("/fit_coords")
        model_coords = fit_coords.get(resolved.model_stage, xr.Dataset())
        fitted = Fitted(
            manifest=mman,
            settings=dict(settings),
            fit_config=fit_cfg,
            groups=groups,
            learned=dict(learned),
            native=native,
            terminal_state=node("/terminal_state") if "terminal_state" in tree else None,
            derived=children("/derived"),
            schema=model_schema,
            coords={str(d): np.asarray(model_coords[d].values) for d in model_coords.coords},
        )
        residuals: dict[str, xr.DataArray] = {}
        if "residuals" in tree:
            residuals = {str(k): v for k, v in node("/residuals").data_vars.items()}
        thin = np.asarray(node("/thin")["index"].values) if "thin" in tree else None
        return cls(
            spec=spec,
            resolved=resolved,
            raw_schema=raw_schema,
            states=states,
            fit_coords=fit_coords,
            model=fitted,
            manifest=manifest,
            thin=thin,
            side_tables=children("/side_tables"),
            residuals=residuals,
        )


def new_manifest(
    *,
    spec: ModelSpec,
    resolved: ResolvedSpec,
    model: Fitted,
    states: Mapping[str, StepState],
    role: Literal["production", "evaluation"],
    data_hash: str | None,
    fit_info: Mapping[str, JsonValue] | None = None,
) -> BundleManifest:
    """A manifest with a deterministic content-derived bundle id."""
    steps: list[dict[str, JsonValue]] = [
        {
            "id": sid,
            "ref": f"{st.step_type}@{st.step_version}",
            "settings_hash": st.settings_hash,
        }
        for sid, st in states.items()
    ]
    ident = json.dumps(
        [resolved.spec_hash, data_hash, role, model.manifest.id, model.manifest.version, steps],
        sort_keys=True,
    )
    return BundleManifest(
        bundle_id=hashlib.sha256(ident.encode()).hexdigest()[:32],
        role=role,
        created_at=datetime.now(UTC).isoformat(),
        spec_hash=resolved.spec_hash,
        data_hash=data_hash,
        plugin={
            "id": model.manifest.id,
            "version": model.manifest.version,
            "representation": model.manifest.capabilities.representation.value,
        },
        steps=steps,
        fit=dict(fit_info or {}),
        lib_versions=lib_versions(),
    )
