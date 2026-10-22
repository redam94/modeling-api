"""The checks behind ``mapi plugin validate`` and ``mapi plugin doctor`` (v0.18).

Both import plugin code, so the CLI runs them in a subprocess
(``python -m mapi_cli.plugin_doctor validate PATH`` or ``... doctor``) that prints one JSON
report on stdout; whatever the plugin prints goes to stderr.

* :func:`validate_package` loads only the entry points a package declares in its
  ``pyproject.toml`` (installed or not) and runs the static checks of every export: the
  namespace rules of :mod:`mapi_spec.namespaces`, complete About cards, safe settings and
  parameter schemas (:mod:`mapi_spec.schema_safety`, ``refuse`` mode), the plot recipe kit
  (:func:`mapi_plots.testing.check_recipe`), the plugin node rules
  (:func:`mapi_spec.workflow_nodes.plugin_contract_issues`) and the template kit
  (:func:`mapi_workflows.kit.check_template`). It returns the catalog fragment the package
  adds. The data checks (:func:`~mapi_sdk.testing.check_model`, ``check_step``,
  ``check_node``) need fixtures: they are the package's own tests (``mapi plugin test``).
* :func:`doctor_report` looks at every installed distribution: the entry points of all six
  groups, every load diagnostic (uncapped), the catalog the runner would describe and its
  digest, the image routing of the extension namespaces, and whether the image digest
  covers the extensions (so the server describes again when they change).
  :func:`compare_server` adds the server comparison (the CLI fetches the server catalog).

Exit codes: :data:`EXIT_OK` (0) everything passed, :data:`EXIT_PROBLEMS` (1) a check found
a problem, :data:`EXIT_BROKEN` (2) the package or an entry point could not be loaded.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import unquote, urlparse

from mapi_spec.catalog import (
    Catalog,
    CatalogDiagnostic,
    PlotCatalogEntry,
    StepCatalogEntry,
    TemplateCatalogEntry,
)
from mapi_spec.namespaces import check_namespace, is_builtin_distribution
from mapi_spec.run import catalog_digest
from mapi_spec.schema_safety import sanitize_schema
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation

__all__ = [
    "ENTRY_POINT_GROUPS",
    "EXIT_BROKEN",
    "EXIT_OK",
    "EXIT_PROBLEMS",
    "Check",
    "compare_server",
    "doctor_report",
    "validate_package",
]

EXIT_OK: Final = 0
EXIT_PROBLEMS: Final = 1
EXIT_BROKEN: Final = 2

ENTRY_POINT_GROUPS: Final[tuple[str, ...]] = (
    "mapi.models",
    "mapi.steps",
    "mapi.plots",
    "mapi.workflow_nodes",
    "mapi.workflow_templates",
    "mapi.governance_checks",
)
"""Every entry-point group the platform reads (the SDK registry reads the first three)."""

_REGISTRY_GROUPS: Final = frozenset({"mapi.models", "mapi.steps", "mapi.plots"})
_EXTRA_LIST: Final = Path(".local") / "extra-plugins.txt"
"""The out-of-tree plugin sources ``make plugin-dev`` adds to a checkout's image digest."""
_CHECKOUT_SOURCES: Final = ("packages", "plugins")
"""Workspace source roots the checkout digest covers (``mapi_server.settings``)."""
_EXTENSIONS_LOCK: Final = Path("/opt/mapi/extensions.lock")
"""Where a custom image (``images/extend``) records its extension wheels."""
_MAX_DETAIL: Final = 2000

Status = Literal["pass", "fail", "warn", "skip"]


@dataclass(slots=True)
class Check:
    """One check of one export: ``kind`` (model, step, plot, node, template, ...),
    ``subject`` (an id or an entry point) and the check ``name``."""

    kind: str
    subject: str
    name: str
    status: Status
    detail: str = ""


@dataclass(slots=True)
class _EntryPoint:
    group: str
    name: str
    value: str
    distribution: str | None = None
    builtin: bool = False
    loaded: bool = True
    error: str | None = None


@dataclass(slots=True)
class _Validation:
    checks: list[Check] = field(default_factory=list)
    entry_points: list[_EntryPoint] = field(default_factory=list)
    broken: bool = False

    def add(
        self, kind: str, subject: str, name: str, problem: str | None, *, warn: bool = False
    ) -> None:
        status: Status = "pass" if problem is None else ("warn" if warn else "fail")
        self.checks.append(Check(kind, subject, name, status, _clip(problem or "")))

    def skip(self, kind: str, subject: str, name: str, why: str) -> None:
        self.checks.append(Check(kind, subject, name, "skip", why))


def _clip(text: str, limit: int = _MAX_DETAIL) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _exc(exc: BaseException) -> str:
    return _clip(f"{type(exc).__name__}: {exc}")


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """Plugin code may print: keep stdout for the JSON report."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


# =============================================================================== validate


def _read_pyproject(path: Path) -> tuple[str, list[_EntryPoint]]:
    doc = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
    project = doc.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("name"), str):
        raise ValueError("pyproject.toml has no [project] name")
    dist = str(project["name"])
    eps: list[_EntryPoint] = []
    groups = project.get("entry-points", {})
    if isinstance(groups, dict):
        for group, table in groups.items():
            if not str(group).startswith("mapi.") or not isinstance(table, dict):
                continue
            for name, value in table.items():
                eps.append(_EntryPoint(str(group), str(name), str(value), dist))
    return dist, eps


def _load(ep: _EntryPoint) -> Any:
    obj = importlib.metadata.EntryPoint(ep.name, ep.value, ep.group).load()
    # like the SDK registry and the workflow loaders: a factory is called, a class, a
    # mapping or an export object (a recipe, a NodeExport) is kept
    exported = isinstance(obj, type | Mapping) or hasattr(obj, "catalog_entry")
    if callable(obj) and not exported and not hasattr(obj, "contract"):
        obj = obj()
    return obj


def _items(obj: Any) -> list[Any]:
    """The exports of an entry point (a list is flattened; a single object is one)."""
    if isinstance(obj, list | tuple | set | frozenset):
        return [item for o in obj for item in _items(o)]
    return [obj]


def _plugin_id(obj: Any) -> str:
    pid = getattr(obj, "id", None)
    return pid if isinstance(pid, str) else repr(obj)[:128]


def _schema_problem(schema: Mapping[str, Any], where: str) -> str | None:
    _, issues = sanitize_schema(schema, mode="refuse", where=where)
    if not issues:
        return None
    more = f"; {len(issues) - 1} more" if len(issues) > 1 else ""
    return f"{issues[0].message} (at {issues[0].pointer or where}){more}"


def _about_problem(about: Any) -> str | None:
    from mapi_spec.about import about_issues

    problems = about_issues(about)
    return "; ".join(problems) if problems else None


def _check_registry_item(v: _Validation, group: str, item: Any, dist: str, registry: Any) -> None:
    kind = {"mapi.models": "model", "mapi.steps": "step", "mapi.plots": "plot"}[group]
    subject = _plugin_id(item)
    try:
        registry.register(item)
    except Exception as exc:  # plugin code: the registration error is the check result
        v.add(kind, subject, "registers", _exc(exc))
        return
    v.add(kind, subject, "registers", None)
    v.add(kind, subject, "namespace", check_namespace(subject, dist))
    if kind == "plot":
        _check_recipe(v, item)
        return
    if kind == "model":
        manifest = item.manifest()
        v.add(kind, subject, "about", _about_problem(manifest.about))
        v.add(
            kind,
            subject,
            "settings_schema",
            _schema_problem(manifest.settings_schema, "/settings_schema"),
        )
        v.add(kind, subject, "fit_schema", _schema_problem(manifest.fit_schema, "/fit_schema"))
    else:
        entry = item.catalog_entry()
        v.add(kind, subject, "about", _about_problem(entry.about))
        v.add(
            kind,
            subject,
            "settings_schema",
            _schema_problem(entry.settings_schema, "/settings_schema"),
        )


def _check_recipe(v: _Validation, recipe: Any) -> None:
    subject = _plugin_id(recipe)
    try:
        from mapi_plots.testing import check_recipe
    except ImportError as exc:  # pragma: no cover - a recipe module imports mapi_plots
        v.skip("plot", subject, "recipe_kit", f"mapi-plots[recipes] is not installed: {exc}")
        return
    try:
        report = check_recipe(recipe, raise_on_failure=False)
    except Exception as exc:  # the kit itself refused the object (not a Recipe)
        v.add("plot", subject, "recipe_kit", _exc(exc))
        return
    for name, reason in report.checks:
        if name != "namespace":  # checked above, with the distribution from pyproject.toml
            v.add("plot", subject, name, reason)
    for name, reason in getattr(report, "warnings", []):
        v.add("plot", subject, name, reason, warn=True)


def _check_nodes(v: _Validation, ep: _EntryPoint, obj: Any, dist: str) -> list[NodeImplementation]:
    from mapi_spec.workflow_nodes import plugin_contract_issues

    if isinstance(obj, Mapping):
        v.add(
            "node",
            f"{ep.group}:{ep.name}",
            "export",
            "the entry point exports a mapping of node ids to functions (deprecated): export "
            "NodeExport objects (a contract and its function) instead",
            warn=True,
        )
        return []
    valid: list[NodeImplementation] = []
    for item in _items(obj):
        contract = getattr(item, "contract", None)
        if not isinstance(contract, NodeImplementation):
            v.add(
                "node",
                f"{ep.group}:{ep.name}",
                "export",
                f"exported {type(item).__name__}, not a NodeExport with a NodeImplementation "
                "contract",
            )
            continue
        subject = contract.id
        if not callable(getattr(item, "fn", None)):
            v.add("node", subject, "function", "fn is not callable")
            continue
        issues = plugin_contract_issues(contract, distribution=dist)
        v.add("node", subject, "contract", "; ".join(i.message for i in issues) or None)
        if not issues:
            valid.append(contract)
    return valid


def _check_templates(
    v: _Validation, ep: _EntryPoint, obj: Any, dist: str, nodes: Mapping[str, NodeImplementation]
) -> list[TemplateCatalogEntry]:
    try:
        from mapi_workflows.kit import check_template
    except ImportError as exc:
        v.skip(
            "template",
            f"{ep.group}:{ep.name}",
            "template_kit",
            f"mapi-workflows[runtime] is not installed, so the templates were not checked: {exc}",
        )
        return []
    valid: list[TemplateCatalogEntry] = []
    for item in [obj] if isinstance(obj, Mapping) else _items(obj):
        raw = item.model_dump(mode="json") if isinstance(item, TemplateCatalogEntry) else item
        subject = str(raw.get("id")) if isinstance(raw, Mapping) else f"{ep.group}:{ep.name}"
        if not isinstance(item, TemplateCatalogEntry | Mapping):
            v.add(
                "template",
                subject,
                "export",
                f"exported {type(item).__name__}, not a TemplateCatalogEntry",
            )
            continue
        issues = check_template(item, nodes=nodes, distribution=dist)
        v.add(
            "template",
            subject,
            "template",
            "; ".join(f"{i.pointer or '/'}: {i.message}" for i in issues) or None,
        )
        if not issues:
            valid.append(TemplateCatalogEntry.model_validate(raw))
    return valid


def _check_governance(v: _Validation, ep: _EntryPoint, obj: Any) -> None:
    subject = f"{ep.group}:{ep.name}"
    if not isinstance(obj, Mapping):
        v.add(
            "governance_check",
            subject,
            "export",
            f"exported {type(obj).__name__}, not a mapping of check ids to functions",
        )
        return
    bad = sorted(str(k) for k, fn in obj.items() if not callable(fn))
    v.add("governance_check", subject, "export", f"not callable: {', '.join(bad)}" if bad else None)


def _provided(entries: Iterable[Any], dist: str) -> list[Any]:
    provider = None if is_builtin_distribution(dist) else dist[:128]
    return [e if provider is None else e.model_copy(update={"provider": provider}) for e in entries]


def validate_package(path: Path) -> dict[str, Any]:
    """Load the entry points ``path/pyproject.toml`` declares and check every export.

    Returns the JSON report: ``distribution``, ``entry_points`` (each with ``loaded`` and
    ``error``), ``checks`` (kind, subject, name, status, detail), ``catalog`` (the fragment
    the package adds: steps, models, plots, workflow nodes and templates, with ``provider``)
    and ``exit_code``."""
    v = _Validation()
    try:
        dist, eps = _read_pyproject(path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return {
            "path": str(path),
            "ok": False,
            "exit_code": EXIT_BROKEN,
            "error": f"cannot read the package: {_exc(exc)}",
            "checks": [],
        }
    v.entry_points = eps
    if not eps:
        v.add("package", dist, "entry_points", "pyproject.toml declares no mapi.* entry points")
    from mapi_sdk.registry import Registry

    registry = Registry()
    nodes: list[NodeImplementation] = []
    templates: list[tuple[_EntryPoint, Any]] = []
    unknown = sorted({ep.group for ep in eps} - set(ENTRY_POINT_GROUPS))
    for group in unknown:
        v.add(
            "package", dist, "groups", f"the platform does not read the group {group!r}", warn=True
        )
    for ep in eps:
        if ep.group not in ENTRY_POINT_GROUPS:
            continue
        try:
            with _quiet():
                obj = _load(ep)
        except Exception as exc:  # report and keep going: one bad entry point
            ep.loaded, ep.error = False, _exc(exc)
            v.broken = True
            v.add("entry_point", f"{ep.group}:{ep.name}", "loads", ep.error)
            continue
        v.add("entry_point", f"{ep.group}:{ep.name}", "loads", None)
        with _quiet():
            if ep.group in _REGISTRY_GROUPS:
                for item in _items(obj):
                    _check_registry_item(v, ep.group, item, dist, registry)
            elif ep.group == "mapi.workflow_nodes":
                nodes += _check_nodes(v, ep, obj, dist)
            elif ep.group == "mapi.workflow_templates":
                templates.append((ep, obj))  # after the nodes: a template may use them
            else:
                _check_governance(v, ep, obj)
    merged = dict(NODE_IMPLEMENTATIONS)
    for c in nodes:
        merged.setdefault(c.id, c)
    valid_templates: list[TemplateCatalogEntry] = []
    for ep, obj in templates:
        with _quiet():
            valid_templates += _check_templates(v, ep, obj, dist, merged)

    with _quiet():
        base = registry.catalog()
    catalog = Catalog(
        steps=_provided(base.steps, dist),
        models=_provided(base.models, dist),
        plots=_provided(base.plots, dist),
        workflow_nodes=sorted(nodes, key=lambda c: c.id),
        workflow_templates=sorted(_provided(valid_templates, dist), key=lambda t: t.id),
    )
    failed = any(c.status == "fail" for c in v.checks)
    code = EXIT_BROKEN if v.broken else EXIT_PROBLEMS if failed else EXIT_OK
    return {
        "path": str(path),
        "distribution": dist,
        "ok": code == EXIT_OK,
        "exit_code": code,
        "entry_points": [asdict(ep) for ep in eps],
        "checks": [asdict(c) for c in v.checks],
        "catalog": catalog.model_dump(mode="json"),
    }


# ================================================================================= doctor


def _installed_entry_points() -> list[_EntryPoint]:
    rows: list[_EntryPoint] = []
    for group in ENTRY_POINT_GROUPS:
        for ep in importlib.metadata.entry_points(group=group):
            dist = getattr(getattr(ep, "dist", None), "name", None)
            rows.append(
                _EntryPoint(group, ep.name, ep.value, dist, builtin=is_builtin_distribution(dist))
            )
    return rows


def _local_catalog() -> tuple[Catalog, list[CatalogDiagnostic], list[str]]:
    """The catalog the runner's ``describe`` would export from this environment (the
    composition of :func:`mapi_runner.catalog_export.full_catalog`), every diagnostic
    uncapped, and what could not be checked."""
    from mapi_sdk.registry import Registry

    skipped: list[str] = []
    registry = Registry()
    registry.load_entry_points()
    base = registry.catalog()
    diags: list[CatalogDiagnostic] = list(registry.diagnostics)
    nodes: list[NodeImplementation] = []
    templates: list[TemplateCatalogEntry] = []
    try:
        from mapi_workflows.runtime.governance import load_check_runners
        from mapi_workflows.runtime.nodes import (
            all_node_contracts,
            load_node_exports,
            load_plugin_templates,
        )
    except ImportError:
        skipped.append(
            "mapi-workflows[runtime] is not installed: workflow nodes, templates and "
            "governance checks were not loaded"
        )
    else:
        exports, node_diags = load_node_exports()
        nodes = [exports[k].contract for k in sorted(exports)]
        templates, template_diags = load_plugin_templates(all_node_contracts(nodes))
        check_diags: list[CatalogDiagnostic] = []
        load_check_runners(diagnostics=check_diags)
        diags += [*node_diags, *template_diags, *check_diags]
    try:
        from mapi_runner.catalog_export import cap_diagnostics

        capped = cap_diagnostics(diags)
    except ImportError:
        capped = diags
    catalog = Catalog(
        steps=base.steps,
        models=base.models,
        plots=base.plots,
        workflow_nodes=nodes,
        workflow_templates=sorted(templates, key=lambda t: t.id),
        diagnostics=capped,
    )
    return catalog, diags, skipped


def _extension_namespaces(catalog: Catalog) -> dict[str, str | None]:
    """``{namespace: provider}`` of every catalog entry from a non-built-in distribution."""
    out: dict[str, str | None] = {}
    entries: list[StepCatalogEntry | PlotCatalogEntry] = [*catalog.steps, *catalog.plots]
    for e in entries:
        if e.provider:
            out.setdefault(e.id.partition(".")[0], e.provider)
    for m in catalog.models:
        if m.provider:
            out.setdefault(m.manifest.id.partition(".")[0], m.provider)
    for t in catalog.workflow_templates:
        if t.provider:
            out.setdefault(t.id.partition("_")[0], t.provider)
    for c in catalog.workflow_nodes:
        if c.plugin:
            out.setdefault(c.plugin, None)
    return out


def _docker_images() -> dict[str, str]:
    raw = os.environ.get("MAPI_DOCKER_IMAGES", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _find_lock(start: Path) -> Path | None:
    for parent in (start, *start.parents):
        if (parent / "uv.lock").is_file():
            return parent / "uv.lock"
    return None


def _source_of(dist: str) -> Path | None:
    """Where an editable (or local) install of ``dist`` lives (``direct_url.json``)."""
    try:
        text = importlib.metadata.distribution(dist).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not text:
        return None
    try:
        url = json.loads(text).get("url", "")
    except json.JSONDecodeError:
        return None
    parsed = urlparse(str(url))
    return Path(unquote(parsed.path)).resolve() if parsed.scheme == "file" else None


def _covered_paths(root: Path) -> list[Path]:
    paths = [(root / p).resolve() for p in _CHECKOUT_SOURCES]
    listed = root / _EXTRA_LIST
    if listed.is_file():
        for line in listed.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                p = Path(line)
                paths.append((p if p.is_absolute() else root / p).resolve())
    return paths


def _digest_coverage(
    extensions: Iterable[str], cwd: Path
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Does the image digest change when an extension changes? (Otherwise the server keeps
    serving the catalog it cached under the old digest.)"""
    dists = sorted(set(extensions))
    problems: list[dict[str, str]] = []
    lock = _find_lock(cwd)
    if lock is not None:  # a source checkout: uv.lock + workspace sources + the extra list
        covered = _covered_paths(lock.parent)
        for d in dists:
            src = _source_of(d)
            if src is not None and any(src == c or c in src.parents for c in covered):
                continue
            where = f" ({src})" if src is not None else ""
            problems.append(
                {
                    "code": "not_in_digest",
                    "message": f"The image digest does not cover {d}{where}, so the server does "
                    "not describe the catalog again when it changes. Run make plugin-dev "
                    "PLUGIN=<its folder>.",
                }
            )
        return {
            "mode": "checkout",
            "lock": str(lock),
            "extra_list": str(lock.parent / _EXTRA_LIST),
        }, problems
    lock_env = os.environ.get("MAPI_EXTENSIONS_LOCK_PATH", "").strip()
    ext_lock = Path(lock_env) if lock_env else _EXTENSIONS_LOCK
    if dists and not ext_lock.is_file():
        problems.append(
            {
                "code": "no_extensions_lock",
                "message": f"Extensions are installed ({', '.join(dists)}) but {ext_lock} is "
                "missing, so the image digest does not change with them. Build the image with "
                "images/extend (make plugin-image).",
            }
        )
    return {"mode": "image", "extensions_lock": str(ext_lock)}, problems


def _catalog_ids(catalog: Catalog) -> dict[str, list[str]]:
    return {
        "models": sorted(f"{m.manifest.id}@{m.manifest.version}" for m in catalog.models),
        "steps": sorted(f"{s.id}@{s.version}" for s in catalog.steps),
        "plots": sorted(f"{p.id}@{p.version}" for p in catalog.plots),
    }


def doctor_report(cwd: Path | None = None) -> dict[str, Any]:
    """Everything installed in this environment, checked (see the module docs)."""
    here = (cwd or Path.cwd()).resolve()
    with _quiet():
        eps = _installed_entry_points()
        catalog, diags, skipped = _local_catalog()
    problems: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = [{"code": "skipped", "message": s} for s in skipped]
    for d in diags:
        row = {
            "code": d.code,
            "message": f"{d.group}: {d.distribution or 'unknown distribution'}"
            f"{' ' + d.id if d.id else ''}: {d.message}",
        }
        (warnings if d.code == "deprecated" else problems).append(row)
    extensions = sorted({ep.distribution for ep in eps if ep.distribution and not ep.builtin})
    images = _docker_images()
    routing = [
        {"namespace": ns, "distribution": provider, "image": images.get(ns)}
        for ns, provider in sorted(_extension_namespaces(catalog).items())
    ]
    coverage, digest_problems = _digest_coverage(extensions, here)
    problems += digest_problems
    return {
        "ok": not problems,
        "exit_code": EXIT_PROBLEMS if problems else EXIT_OK,
        "entry_points": [asdict(ep) for ep in eps],
        "extensions": extensions,
        "diagnostics": [d.model_dump(mode="json") for d in diags],
        "routing": routing,
        "digest": {"catalog_digest": catalog_digest(catalog), **coverage},
        "catalog_ids": _catalog_ids(catalog),
        "problems": problems,
        "warnings": warnings,
    }


def compare_server(report: dict[str, Any], remote: Mapping[str, Any], *, url: str = "") -> None:
    """Add the server comparison to a :func:`doctor_report` (in place).

    ``remote`` is the server's ``GET /v1/catalog`` body. A different catalog digest means
    the server described another environment, or this one before a change (stale): restart
    it (or ``make catalog-refresh``) so it describes the current code again."""
    local_ids: dict[str, list[str]] = report.get("catalog_ids") or {}
    server_ids = {
        "models": sorted(
            f"{m['manifest']['id']}@{m['manifest']['version']}" for m in remote.get("models", [])
        ),
        "steps": sorted(f"{s['id']}@{s['version']}" for s in remote.get("steps", [])),
        "plots": sorted(f"{p['id']}@{p.get('version', '1.0.0')}" for p in remote.get("plots", [])),
    }
    missing = {k: sorted(set(local_ids.get(k, [])) - set(v)) for k, v in server_ids.items()}
    extra = {k: sorted(set(v) - set(local_ids.get(k, []))) for k, v in server_ids.items()}
    local_digest = report.get("digest", {}).get("catalog_digest")
    stale = remote.get("digest") != local_digest
    report["server"] = {
        "url": url,
        "digest": remote.get("digest"),
        "local_digest": local_digest,
        "stale": stale,
        "missing_on_server": {k: v for k, v in missing.items() if v},
        "only_on_server": {k: v for k, v in extra.items() if v},
    }
    if stale:
        listed = [i for ids in missing.values() for i in ids]
        report.setdefault("problems", []).append(
            {
                "code": "stale_catalog",
                "message": "The server's catalog digest differs from this environment's"
                + (f" (not on the server: {', '.join(listed[:5])})" if listed else "")
                + ". Restart the server or run make catalog-refresh so it describes the "
                "current code again.",
            }
        )
        report["ok"] = False
        if report.get("exit_code", EXIT_OK) == EXIT_OK:
            report["exit_code"] = EXIT_PROBLEMS


# =================================================================================== main


def main(argv: list[str] | None = None) -> int:
    """``python -m mapi_cli.plugin_doctor validate PATH | doctor``: print the JSON report."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["validate"] and len(args) == 2:
        report = validate_package(Path(args[1]))
    elif args == ["doctor"]:
        report = doctor_report()
    else:
        print("usage: python -m mapi_cli.plugin_doctor validate PATH | doctor", file=sys.stderr)
        return EXIT_BROKEN
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
