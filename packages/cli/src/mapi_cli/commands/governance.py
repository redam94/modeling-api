"""Governance (v0.5): ``mapi governance …`` (checks and policies), ``mapi validate model …``,
``mapi validations …`` and ``mapi approve …``.

A **policy** lists the checks a model must pass (null tests against shuffled results and
noise drivers, naive benchmarks, residual checks, ...). ``mapi validate model`` runs them on
a model version in the sandbox and shows every result in plain words; a second person with
the reviewer capability approves with ``mapi approve`` (a signed-in session: API tokens can
never approve); ``mapi validations pack`` exports the evidence for an auditor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_spec.api import CheckResult, GovernancePolicy, ModelValidation
from mapi_spec.governance import ValidationStatus
from mapi_spec.loader import parse_text
from rich.text import Text

from mapi_cli.about import print_about
from mapi_cli.commands.workflows import report_validation
from mapi_cli.output import Output, safe
from mapi_cli.resolve import resolve_model
from mapi_cli.state import EXIT_FAILURE, cli_errors, confirm, state

app = typer.Typer(
    help="Governance: the check catalog and the policies models must pass.",
    no_args_is_help=True,
)
policies = typer.Typer(help="Project and tenant-wide governance policies.", no_args_is_help=True)
app.add_typer(policies, name="policies")

validate_app = typer.Typer(
    help="Validate a model version against the governance policies (or a workflow file).",
    no_args_is_help=True,
)
validations = typer.Typer(
    help="Validations: the review queue, evidence, resubmission, deprecation, evidence packs.",
    no_args_is_help=True,
)

CHECK_STYLE = {
    "pass": "green",
    "warn": "yellow",
    "fail": "bold red",
    "error": "red",
    "skipped": "dim",
}
VALIDATION_STYLE = {
    "validated": "green",
    "checks_passed": "green",
    "awaiting_approval": "yellow",
    "checks_running": "cyan",
    "checks_failed": "bold red",
    "rejected": "bold red",
    "deprecated": "dim",
    "draft": "dim",
}
_BAD = {ValidationStatus.CHECKS_FAILED, ValidationStatus.REJECTED}


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.4g}"


def print_results(out: Output, results: list[CheckResult], *, title: str = "Checks") -> None:
    out.table(
        ["check", "result", "value", "threshold", "what it means"],
        [
            (
                r.check,
                safe(r.status.value, CHECK_STYLE.get(r.status.value, "")),
                _num(r.value),
                _num(r.threshold),
                safe(r.message_plain)
                + (
                    Text("\n→ ") + safe(r.suggested_action, "italic")
                    if r.suggested_action
                    else Text("")
                ),
            )
            for r in results
        ],
        title=title,
    )


def print_validation(out: Output, v: ModelValidation) -> None:
    if out.json_mode:
        out.emit_json(v)
        return
    out.kv(
        [
            ("validation", v.id),
            ("model", f"{v.model_id} v{v.version}"),
            ("status", safe(v.status.value, VALIDATION_STYLE.get(v.status.value, ""))),
            ("policies", ", ".join(v.policies)),
            ("failing", ", ".join(v.failing_checks)),
            (
                "approvals",
                f"{sum(a.decision == 'approve' for a in v.approvals)}/{v.approvals_required}"
                if v.require_approval
                else None,
            ),
            ("governance run", v.workflow_run_id),
            ("submitted", f"{_when(v.submitted_at)} by {v.submitted_by or '-'}"),
        ]
    )
    if v.results:
        print_results(out, v.results)
    if v.status is ValidationStatus.AWAITING_APPROVAL:
        out.info(f"a reviewer (not the submitter) decides with: mapi approve {v.id}")


def _policy_rows(items: list[GovernancePolicy]) -> list[tuple[object, ...]]:
    return [
        (
            p.id,
            p.name,
            p.scope,
            p.version,
            ", ".join(c.check for c in p.checks),
            "yes" if p.require_approval else "no",
            "yes" if p.require_validated_for_promotion else "no",
            "yes" if p.active else "retired",
        )
        for p in items
    ]


_POLICY_COLUMNS = [
    "id",
    "name",
    "scope",
    "version",
    "checks",
    "approval",
    "gates promotion",
    "active",
]


def _load(file: Path) -> dict[str, Any]:
    doc = parse_text(file.read_text("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{file} must hold one policy (a mapping)")
    return doc


# ======================================================================= checks


@app.command("checks")
@cli_errors
def checks(
    ctx: typer.Context,
    about: Annotated[
        str | None, typer.Option("--about", help="Show one check's About card")
    ] = None,
    expert: Annotated[bool, typer.Option("--expert", help="The statistics behind it")] = False,
) -> None:
    """The checks a policy can require, in plain words."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        items = list(api.governance.checks(limit=200))
    if about is not None:
        found = next((c for c in items if c.id == about), None)
        if found is None:
            raise ValueError(f"no governance check {about!r} ({', '.join(c.id for c in items)})")
        if out.json_mode:
            out.emit_json(found)
            return
        print_about(out, found.about, item_id=found.id, expert=expert)
        out.kv(
            [
                ("metric", f"{found.metric} ({found.direction.value})"),
                ("passes / warns at", f"{_num(found.warn)} / {_num(found.fail)}"),
                ("default severity", found.default_severity.value),
                ("applies to", ", ".join(found.families or []) or "every model"),
            ]
        )
        return
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["check", "category", "it asks", "severity"],
        [(c.id, str(c.category), c.about.summary_plain, c.default_severity.value) for c in items],
    )


# ===================================================================== policies


@policies.command("ls")
@cli_errors
def policies_ls(
    ctx: typer.Context,
    scope: Annotated[
        str | None, typer.Option("--scope", help="tenant or project (default: both)")
    ] = None,
    tenant: Annotated[
        str | None, typer.Option("--tenant", help="List a tenant's own policies instead")
    ] = None,
) -> None:
    """Policies in force in the project (tenant-wide first), or a tenant's."""
    st = state(ctx)
    if scope is not None and scope not in ("tenant", "project"):
        raise ValueError("--scope must be tenant or project")
    with st.client() as api:
        items = list(
            api.governance.policies(
                scope=scope,  # type: ignore[arg-type]
                tenant_id=tenant,
                limit=200,
            )
        )
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(_POLICY_COLUMNS, _policy_rows(items))


@policies.command("get")
@cli_errors
def policies_get(
    ctx: typer.Context,
    policy_id: Annotated[str, typer.Argument(help="Policy id")],
    tenant: Annotated[str | None, typer.Option("--tenant")] = None,
) -> None:
    """A policy's latest version with every check and threshold."""
    st = state(ctx)
    with st.client() as api:
        p = api.governance.policy(policy_id, tenant_id=tenant)
    out = st.output
    if out.json_mode:
        out.emit_json(p)
        return
    out.kv(
        [
            ("policy", p.id),
            ("name", p.name),
            ("scope", p.scope),
            ("version", p.version),
            ("applies to", p.applies_to.model_dump_json(exclude_defaults=True)),
            ("approval", f"{p.approvals_required} reviewer(s)" if p.require_approval else "no"),
            ("gates promotion", "yes" if p.require_validated_for_promotion else "no"),
            ("active", "yes" if p.active else "retired"),
            ("description", p.description),
        ]
    )
    out.table(
        ["check", "severity", "warn", "fail", "params"],
        [
            (
                c.check,
                c.severity.value,
                _num(c.thresholds.warn) if c.thresholds else "default",
                _num(c.thresholds.fail) if c.thresholds else "default",
                c.params or "",
            )
            for c in p.checks
        ],
    )


@policies.command("create")
@cli_errors
def policies_create(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="Policy YAML or JSON", exists=True, dir_okay=False)],
    tenant: Annotated[
        str | None, typer.Option("--tenant", help="Create a tenant-wide policy (tenant admins)")
    ] = None,
) -> None:
    """Add a policy from a file (name, applies_to, checks, require_approval, ...)."""
    st = state(ctx)
    with st.client() as api:
        p = api.governance.create_policy(_load(file), tenant_id=tenant)
    if st.output.json_mode:
        st.output.emit_json(p)
    else:
        st.output.success(f"policy {p.id} ({p.name}) version {p.version}, {p.scope}-wide")


@policies.command("update")
@cli_errors
def policies_update(
    ctx: typer.Context,
    policy_id: Annotated[str, typer.Argument(help="Policy id")],
    file: Annotated[Path, typer.Argument(help="Policy YAML or JSON", exists=True, dir_okay=False)],
    tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    expected_version: Annotated[
        int | None, typer.Option("--expected-version", min=1, help="Refuse if it changed since")
    ] = None,
) -> None:
    """Save the file as the policy's next version."""
    st = state(ctx)
    with st.client() as api:
        p = api.governance.update_policy(
            policy_id, _load(file), tenant_id=tenant, expected_version=expected_version
        )
    if st.output.json_mode:
        st.output.emit_json(p)
    else:
        st.output.success(f"policy {p.id} is now version {p.version}")


@policies.command("retire")
@cli_errors
def policies_retire(
    ctx: typer.Context,
    policy_id: Annotated[str, typer.Argument(help="Policy id")],
    tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Retire a policy (its versions stay for audit)."""
    st = state(ctx)
    confirm(st, f"Retire policy {policy_id}? Models will no longer be held to it.", yes=yes)
    with st.client() as api:
        api.governance.retire_policy(policy_id, tenant_id=tenant)
    st.output.success(f"retired policy {policy_id}")


@policies.command("versions")
@cli_errors
def policies_versions(
    ctx: typer.Context, policy_id: Annotated[str, typer.Argument(help="Policy id")]
) -> None:
    """Every version of a project policy, newest first."""
    st = state(ctx)
    with st.client() as api:
        items = list(api.governance.policy_versions(policy_id, limit=200))
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["version", "name", "checks", "by", "when"],
        [
            (
                p.version,
                p.name,
                ", ".join(c.check for c in p.checks),
                p.created_by or "-",
                _when(p.created_at),
            )
            for p in items
        ],
    )


# ===================================================================== validate


@validate_app.command("model")
@cli_errors
def validate_model(
    ctx: typer.Context,
    model: Annotated[str, typer.Argument(help="model://project/name@vN, name@vN or name")],
    version: Annotated[int | None, typer.Option("--version", "-v", min=1)] = None,
    policy: Annotated[
        list[str] | None, typer.Option("--policy", help="Only these policies (repeat)")
    ] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    dataset: Annotated[
        str | None, typer.Option("--dataset", help="dataset:// for the data checks")
    ] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the checks")] = True,
    timeout: Annotated[float, typer.Option("--timeout", min=1)] = 3600.0,
) -> None:
    """Run the governance checks on a model version and show every result.

    Exit status 1 when a blocking check failed (``checks_failed``)."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        model_id, ver = resolve_model(api, model, version)
        v = api.validations.request(
            model_id, ver, policy_ids=policy or [], note=note, dataset=dataset
        )
        if wait and v.status is ValidationStatus.CHECKS_RUNNING:
            out.info(f"validation {v.id}: checks running in the sandbox ({v.workflow_run_id})")
            v = api.validations.wait(v.id, timeout=timeout)
    print_validation(out, v)
    if v.status in _BAD:
        raise typer.Exit(EXIT_FAILURE)


@validate_app.command("workflow")
@cli_errors
def validate_workflow(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help="Workflow YAML or JSON", exists=True, dir_okay=False)
    ],
) -> None:
    """Check a workflow file on the server (same as ``mapi workflows validate``)."""
    st = state(ctx)
    with st.client() as api:
        v = api.workflows.validate(file)
    report_validation(st.output, str(file), v)
    if not v.valid:
        raise typer.Exit(EXIT_FAILURE)


# ================================================================== validations


@validations.command("ls")
@cli_errors
def validations_ls(
    ctx: typer.Context,
    status: Annotated[ValidationStatus | None, typer.Option("--status")] = None,
    queue: Annotated[
        bool, typer.Option("--queue", help="The reviewers' queue (awaiting approval)")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
) -> None:
    """Validations of the project, most recently changed first."""
    st = state(ctx)
    wanted = ValidationStatus.AWAITING_APPROVAL if queue else status
    with st.client() as api:
        items = api.validations.list(status=wanted, limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["validation", "model", "status", "failing", "submitted by", "updated"],
        [
            (
                v.id,
                f"{v.model_id} v{v.version}",
                safe(v.status.value, VALIDATION_STYLE.get(v.status.value, "")),
                ", ".join(v.failing_checks),
                v.submitted_by or "-",
                _when(v.updated_at),
            )
            for v in items
        ],
    )


@validations.command("get")
@cli_errors
def validations_get(
    ctx: typer.Context, validation_id: Annotated[str, typer.Argument(help="Validation id")]
) -> None:
    """A validation: status, every result, approvals and history."""
    st = state(ctx)
    with st.client() as api:
        v = api.validations.get(validation_id)
    print_validation(st.output, v)
    if not st.output.json_mode and v.history:
        st.output.table(
            ["from", "to", "event", "by", "when", "note"],
            [
                (
                    h.from_status.value,
                    h.to_status.value,
                    h.event.value,
                    h.actor_id or "-",
                    _when(h.at),
                    h.note or "",
                )
                for h in v.history
            ],
            title="History",
        )


@validations.command("evidence")
@cli_errors
def validations_evidence(
    ctx: typer.Context, validation_id: Annotated[str, typer.Argument(help="Validation id")]
) -> None:
    """Every check result with its evidence (numbers, tables)."""
    st = state(ctx)
    with st.client() as api:
        items = list(api.validations.evidence(validation_id, limit=200))
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    print_results(out, items, title="Evidence")
    for r in items:
        for e in r.evidence:
            value = e.value if e.value is not None else (e.ref or e.inline)
            out.out.print(
                Text(f"  {r.check} ", style="dim")
                + safe(f"{e.name}: {value}")
                + (Text(" - ") + safe(e.caption_plain) if e.caption_plain else Text(""))
            )


@validations.command("cancel")
@cli_errors
def validations_cancel(
    ctx: typer.Context,
    validation_id: Annotated[str, typer.Argument(help="Validation id")],
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Stop running checks (back to draft)."""
    st = state(ctx)
    with st.client() as api:
        print_validation(st.output, api.validations.cancel(validation_id, note=note))


@validations.command("resubmit")
@cli_errors
def validations_resubmit(
    ctx: typer.Context,
    validation_id: Annotated[str, typer.Argument(help="Validation id")],
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Run the checks again after a failure or a rejection."""
    st = state(ctx)
    with st.client() as api:
        print_validation(st.output, api.validations.resubmit(validation_id, note=note))


@validations.command("deprecate")
@cli_errors
def validations_deprecate(
    ctx: typer.Context,
    validation_id: Annotated[str, typer.Argument(help="Validation id")],
    reason: Annotated[str, typer.Option("--reason", help="Why it should no longer be used")],
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Retire a validated model version (audited with the reason)."""
    st = state(ctx)
    confirm(st, f"Deprecate the model version of {validation_id}?", yes=yes)
    with st.client() as api:
        print_validation(st.output, api.validations.deprecate(validation_id, reason))


@validations.command("pack")
@cli_errors
def validations_pack(
    ctx: typer.Context,
    validation_id: Annotated[str, typer.Argument(help="Validation id")],
    dest: Annotated[Path, typer.Option("--out", "-o", help="pack.html (static page) or pack.json")],
    no_plots: Annotated[bool, typer.Option("--no-plots")] = False,
) -> None:
    """Export the evidence pack for an auditor (self-contained HTML, or JSON)."""
    st = state(ctx)
    with st.client() as api:
        path = api.validations.download_evidence_pack(
            validation_id, dest, include_plots=not no_plots
        )
    st.output.success(f"wrote {path} ({path.stat().st_size} bytes)")


# ======================================================================= approve


@cli_errors
def approve(
    ctx: typer.Context,
    validation_id: Annotated[str, typer.Argument(help="Validation awaiting approval")],
    reject: Annotated[
        bool, typer.Option("--reject", help="Reject instead (needs --comment)")
    ] = False,
    comment: Annotated[str | None, typer.Option("--comment", "-c")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask")] = False,
) -> None:
    """Approve (or --reject) a validated model version as a reviewer.

    Needs a signed-in session (``mapi login``) of a member with the reviewer capability who
    did not submit it; API tokens can never approve."""
    st = state(ctx)
    if reject and not comment:
        raise ValueError("a rejection needs --comment explaining why")
    with st.client() as api:
        v = api.validations.get(validation_id)
        if not st.output.json_mode:
            print_validation(st.output, v)
        decision = "reject" if reject else "approve"
        confirm(st, f"{decision.capitalize()} {v.model_id} v{v.version}?", yes=yes)
        decided = api.validations.decide(validation_id, decision, comment=comment)  # type: ignore[arg-type]
    out = st.output
    if out.json_mode:
        out.emit_json(decided)
    else:
        style = VALIDATION_STYLE.get(decided.status.value, "")
        out.out.print(Text(f"{decision}d: ", style="bold") + safe(decided.status.value, style))
