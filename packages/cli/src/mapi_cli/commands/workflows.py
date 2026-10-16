"""``mapi workflows …`` (v0.5): validate, save, run and watch workflows; the gallery.

A workflow is a versioned YAML document (``kind: Workflow``) of tests, transforms, branches,
splits, searches, model fits, evaluations, governance checks and reports. The usual loop::

    mapi workflows init ts_readiness_arimax --name weekly-readiness   # a YAML to edit
    mapi workflows validate weekly-readiness.yaml                     # checked by the server
    mapi workflows apply weekly-readiness.yaml                        # create or revise
    mapi workflows run weekly-readiness --input data=dataset://growth/sales@latest --watch

``--watch`` (and ``mapi workflows watch RUN``) shows every node's status live and the
summary when the run ends; the exit status is 1 when the run failed or was cancelled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from mapi_spec.api import Workflow, WorkflowValidation
from mapi_spec.loader import dump_yaml
from mapi_spec.resources import ResourceClass
from mapi_spec.workflow import NodeRunStatus, WorkflowRunStatus
from mapi_spec.workflow_nodes import NodeKind
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES, instantiate_template
from rich.text import Text

from mapi_cli.about import DIFFICULTY_STYLE, print_about, runtime_text
from mapi_cli.output import Output, safe
from mapi_cli.resolve import parse_assignments, resolve_workflow
from mapi_cli.state import EXIT_FAILURE, cli_errors, confirm, state
from mapi_cli.workflow_view import STATUS_STYLE, node_summary, print_run, watch_run

app = typer.Typer(
    help="Workflows: validate, save, run and watch DAGs of tests, transforms and models.",
    no_args_is_help=True,
)
schedules = typer.Typer(help="Cron schedules of a workflow.", no_args_is_help=True)
triggers = typer.Typer(
    help="Run a workflow when a dataset gets a new version.", no_args_is_help=True
)
app.add_typer(schedules, name="schedules")
app.add_typer(triggers, name="triggers")

_FAILED = {"failed", "cancelled"}


def _when(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _show(out: Output, wf: Workflow) -> None:
    out.kv(
        [
            ("workflow", wf.id),
            ("ref", wf.ref),
            ("latest revision", wf.latest_revision),
            ("template", wf.template),
            ("archived", "yes" if wf.archived else None),
            ("description", wf.description),
            ("updated", _when(wf.updated_at)),
        ]
    )


def report_validation(out: Output, label: str, v: WorkflowValidation) -> None:
    if out.json_mode:
        out.emit_json(v)
        return
    if v.valid:
        out.success(f"{label}: valid ({len(v.order)} nodes)")
        out.out.print(Text("order: ", style="dim") + safe(" → ".join(v.order)))
    else:
        out.err.print(Text(f"{label}: invalid", style="bold red"))
    out.issues(v.issues)


# ===================================================================== documents


@app.command("validate")
@cli_errors
def validate(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help="Workflow YAML or JSON", exists=True, dir_okay=False)
    ],
) -> None:
    """Check a workflow file on the server (nothing is saved): unknown nodes, parameters,
    ports, conditions and cycles, each with a JSON pointer."""
    st = state(ctx)
    with st.client() as api:
        v = api.workflows.validate(file)
    report_validation(st.output, str(file), v)
    if not v.valid:
        raise typer.Exit(EXIT_FAILURE)


@app.command("apply")
@cli_errors
def apply(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(help="Workflow YAML or JSON", exists=True, dir_okay=False)
    ],
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
) -> None:
    """Create the workflow named in the file, or save the file as its next revision (an
    unchanged file adds nothing)."""
    st = state(ctx)
    with st.client() as api:
        wf, rev = api.workflows.upsert(file, message=message)
    out = st.output
    if out.json_mode:
        out.emit_json({"workflow": wf, "revision": rev})
        return
    out.success(f"{wf.ref} at revision {rev.revision}")
    report_validation(out, rev.ref, rev.validation)
    if not rev.validation.valid:
        out.warn("this revision cannot run until a new revision fixes it")


@app.command("create")
@cli_errors
def create(
    ctx: typer.Context,
    file: Annotated[
        Path | None, typer.Argument(help="Workflow YAML or JSON", exists=True, dir_okay=False)
    ] = None,
    template: Annotated[
        str | None, typer.Option("--template", "-t", help="Copy a gallery template instead")
    ] = None,
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
) -> None:
    """Create a workflow from a file or a gallery template (revision 1)."""
    st = state(ctx)
    with st.client() as api:
        wf = api.workflows.create(file, template=template, message=message)
    if st.output.json_mode:
        st.output.emit_json(wf)
    else:
        _show(st.output, wf)


@app.command("ls")
@cli_errors
def ls(
    ctx: typer.Context,
    archived: Annotated[bool, typer.Option("--archived", help="Archived workflows")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 50,
) -> None:
    """List the project's workflows."""
    st = state(ctx)
    with st.client() as api:
        items = api.workflows.list(archived=archived, limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["name", "id", "revision", "template", "updated"],
        [(w.name, w.id, w.latest_revision, w.template or "", _when(w.updated_at)) for w in items],
    )


@app.command("get")
@cli_errors
def get(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    revision: Annotated[int | None, typer.Option("--revision", "-r", min=1)] = None,
    yaml_out: Annotated[bool, typer.Option("--yaml", help="Print the document as YAML")] = False,
) -> None:
    """Show a workflow (and with --yaml its document at the latest or a given revision)."""
    st = state(ctx)
    with st.client() as api:
        wf = resolve_workflow(api, workflow)
        rev = api.workflows.revision(wf.id, revision or wf.latest_revision)
    out = st.output
    if out.json_mode:
        out.emit_json({"workflow": wf, "revision": rev})
        return
    if yaml_out:
        typer.echo(dump_yaml(rev.document, exclude_defaults=False), nl=False)
        return
    _show(out, wf)
    report_validation(out, rev.ref, rev.validation)


@app.command("revisions")
@cli_errors
def revisions(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 20,
) -> None:
    """Revisions, newest first, and whether each can run."""
    st = state(ctx)
    with st.client() as api:
        wf = resolve_workflow(api, workflow)
        items = api.workflows.revisions(wf.id, limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["revision", "valid", "message", "by", "when"],
        [
            (
                r.revision,
                safe("yes", "green") if r.validation.valid else safe("no", "red"),
                r.message or "",
                r.created_by or "-",
                _when(r.created_at),
            )
            for r in items
        ],
    )


@app.command("archive")
@cli_errors
def archive(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    undo: Annotated[bool, typer.Option("--undo", help="Unarchive")] = False,
) -> None:
    """Archive a workflow (it can no longer run) or bring it back with --undo."""
    st = state(ctx)
    with st.client() as api:
        wf = api.workflows.update(resolve_workflow(api, workflow).id, archived=not undo)
    st.output.success(f"{wf.ref} {'archived' if wf.archived else 'active'}")


@app.command("rm")
@cli_errors
def rm(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask")] = False,
) -> None:
    """Delete a workflow with its runs, schedules and triggers."""
    st = state(ctx)
    with st.client() as api:
        wf = resolve_workflow(api, workflow)
        confirm(st, f"Delete {wf.ref} with its runs, schedules and triggers?", yes=yes)
        api.workflows.delete(wf.id)
    st.output.success(f"deleted {wf.ref}")


# ======================================================================= gallery


@app.command("templates")
@cli_errors
def templates(ctx: typer.Context) -> None:
    """The gallery of ready-made workflows, each in plain words."""
    st = state(ctx)
    with st.client() as api:
        items = list(api.workflows.templates(limit=50))
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["template", "what it does", "difficulty", "time"],
        [
            (
                Text.assemble(safe(t.about.title, "bold"), "\n", safe(t.id, "dim")),
                t.about.summary_plain,
                safe(t.about.difficulty.value, DIFFICULTY_STYLE.get(t.about.difficulty.value, "")),
                runtime_text(t.about.typical_runtime.value),
            )
            for t in items
        ],
    )
    out.info("`mapi workflows template ID` shows one in full; `mapi workflows init ID` copies it")


@app.command("template")
@cli_errors
def template(
    ctx: typer.Context,
    template_id: Annotated[str, typer.Argument(help="Gallery template id")],
    expert: Annotated[bool, typer.Option("--expert", help="Show the maths and references")] = False,
) -> None:
    """One template's About card and its nodes."""
    st = state(ctx)
    with st.client() as api:
        t = api.workflows.template(template_id)
    out = st.output
    if out.json_mode:
        out.emit_json(t)
        return
    print_about(out, t.about, item_id=t.id, expert=expert)
    nodes = t.document.get("nodes")
    if isinstance(nodes, list):
        out.table(
            ["node", "uses", "runs when"],
            [
                (n.get("id", ""), n.get("use", ""), n.get("when") or "always")
                for n in nodes
                if isinstance(n, dict)
            ],
            title="Nodes",
        )


@app.command("init")
@cli_errors
def init(
    ctx: typer.Context,
    template_id: Annotated[str, typer.Argument(help="Gallery template id")],
    name: Annotated[
        str | None, typer.Option("--name", help="metadata.name (default: the template id)")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--into", help="metadata.project (default: the selected project)")
    ] = None,
    out_file: Annotated[
        Path | None, typer.Option("--out", "-o", help="File to write (default: <name>.yaml)")
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use the templates bundled with the CLI")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite the file")] = False,
) -> None:
    """Write a gallery template as a YAML file to edit, validate and apply."""
    st = state(ctx)
    wf_name = (name or template_id).replace("_", "-")
    if offline:
        if template_id not in WORKFLOW_TEMPLATES:
            raise ValueError(f"no bundled template {template_id!r} ({sorted(WORKFLOW_TEMPLATES)})")
        tpl = WORKFLOW_TEMPLATES[template_id]
        proj = project or "default"
    else:
        with st.client() as api:
            tpl = api.workflows.template(template_id)
            selected = api.project
        proj = project or (selected.split("/", 1)[1] if selected and "/" in selected else "default")
    spec = instantiate_template(tpl, name=wf_name, project=proj)
    text = (
        f"# {tpl.about.title}: {tpl.about.summary_plain}\n"
        f"# from the gallery template {tpl.id}@{tpl.version}; edit, then\n"
        f"#   mapi workflows validate {out_file or wf_name + '.yaml'}\n" + dump_yaml(spec)
    )
    path = out_file or Path(f"{wf_name}.yaml")
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists (use --force to overwrite)")
    path.write_text(text, "utf-8")
    if st.output.json_mode:
        st.output.emit_json({"file": str(path), "template": tpl.id, "name": wf_name})
    else:
        st.output.success(f"wrote {path} ({len(spec.nodes)} nodes)")


# ========================================================================== runs


@app.command("run")
@cli_errors
def run(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    inputs: Annotated[
        list[str] | None,
        typer.Option("--input", "-i", help="name=dataset://project/name@version (repeat)"),
    ] = None,
    params: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="name=value, JSON values allowed (repeat)"),
    ] = None,
    revision: Annotated[int | None, typer.Option("--revision", "-r", min=1)] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Recompute every node")] = False,
    resource_class: Annotated[
        ResourceClass | None, typer.Option("--resource-class", help="S, M, L or XL")
    ] = None,
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Show node status live until the run ends")
    ] = False,
) -> None:
    """Start a run (inputs are pinned to exact versions when it starts)."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        wf = resolve_workflow(api, workflow)
        started = api.workflows.run(
            wf.id,
            inputs=parse_assignments(inputs or [], what="--input", json_values=False),
            params=parse_assignments(params or [], what="--param"),
            revision=revision,
            use_cache=not no_cache,
            resource_class=resource_class,
        )
        if not watch:
            if out.json_mode:
                out.emit_json(started)
            else:
                out.success(f"started workflow run {started.run_id} of {wf.ref}")
                out.info(f"follow it with: mapi workflows watch {started.run_id}")
            return
        out.info(f"started workflow run {started.run_id} of {wf.ref}")
        final = watch_run(api, out, started.run_id)
    if not out.json_mode:
        print_run(out, final)
    if final.status.value in _FAILED:
        raise typer.Exit(EXIT_FAILURE)


@app.command("watch")
@cli_errors
def watch(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Workflow run id")],
    last_event_id: Annotated[
        int | None, typer.Option("--from", help="Resume after this event")
    ] = None,
) -> None:
    """Follow a run live: every node's status, branch decisions, then the summary
    (--json: one JSON event per line)."""
    st = state(ctx)
    with st.client() as api:
        final = watch_run(api, st.output, run_id, last_event_id=last_event_id)
    if not st.output.json_mode:
        print_run(st.output, final)
    if final.status.value in _FAILED:
        raise typer.Exit(EXIT_FAILURE)


@app.command("status")
@cli_errors
def status(
    ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="Workflow run id")]
) -> None:
    """A run's status, nodes, decisions and outputs."""
    st = state(ctx)
    with st.client() as api:
        print_run(st.output, api.workflows.get_run(run_id))


@app.command("runs")
@cli_errors
def runs(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    run_status: Annotated[WorkflowRunStatus | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1)] = 20,
) -> None:
    """Runs of a workflow, newest first."""
    st = state(ctx)
    with st.client() as api:
        wf = resolve_workflow(api, workflow)
        items = api.workflows.runs(wf.id, status=run_status, limit=min(limit, 200)).take(limit)
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["run", "revision", "status", "trigger", "nodes", "started"],
        [
            (
                r.run_id,
                r.revision,
                safe(r.status.value, STATUS_STYLE.get(r.status.value, "")),
                r.trigger.value,
                f"{sum(1 for n in r.nodes if n.status.done)}/{len(r.nodes)}",
                _when(r.started_at or r.created_at),
            )
            for r in items
        ],
    )


@app.command("nodes")
@cli_errors
def nodes(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Workflow run id")],
    node_status: Annotated[NodeRunStatus | None, typer.Option("--status")] = None,
) -> None:
    """The nodes of a run with what each found."""
    st = state(ctx)
    with st.client() as api:
        items = list(api.workflows.nodes(run_id, status=node_status, limit=200))
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["node", "uses", "status", "attempt", "found"],
        [
            (
                n.node_id,
                n.use,
                safe(n.status.value, STATUS_STYLE.get(n.status.value, "")),
                n.attempt,
                node_summary(n),
            )
            for n in items
        ],
    )


@app.command("node")
@cli_errors
def node(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Workflow run id")],
    node_id: Annotated[str, typer.Argument(help="Node id")],
) -> None:
    """One node in full: resolved parameters, result, outputs and child runs."""
    st = state(ctx)
    with st.client() as api:
        n = api.workflows.node(run_id, node_id)
    out = st.output
    if out.json_mode:
        out.emit_json(n)
        return
    out.kv(
        [
            ("node", n.node_id),
            ("uses", n.use),
            ("status", safe(n.status.value, STATUS_STYLE.get(n.status.value, ""))),
            ("attempt", n.attempt),
            ("child runs", ", ".join(n.child_runs)),
            ("cached from", n.cached_from),
            ("skipped", n.skipped_reason),
            ("params", json.dumps(n.params, indent=2) if n.params else None),
            ("result", json.dumps(n.result, indent=2, default=str) if n.result else None),
            (
                "outputs",
                "\n".join(
                    f"{port}: {o.type.value} {o.ref or o.object or ''}".rstrip()
                    for port, o in n.outputs.items()
                ),
            ),
        ]
    )
    if n.error is not None:
        out.err.print(
            Text("error ", style="bold red")
            + safe(n.error.code.value)
            + Text(": ")
            + safe(n.error.message)
        )


@app.command("output")
@cli_errors
def output(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Workflow run id")],
    node_id: Annotated[str, typer.Argument(help="Node id")],
    port: Annotated[str, typer.Argument(help="Output port, e.g. data or model")],
    dest: Annotated[Path, typer.Option("--out", "-o", help="Where to write the file")],
) -> None:
    """Download a node output's file (a derived dataset, a fitted bundle), verified."""
    st = state(ctx)
    with st.client() as api:
        path = api.workflows.download_output(run_id, node_id, port, dest)
    st.output.success(f"wrote {path} ({path.stat().st_size} bytes)")


@app.command("cancel")
@cli_errors
def cancel(
    ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="Workflow run id")]
) -> None:
    """Cancel a run and its running children."""
    st = state(ctx)
    with st.client() as api:
        r = api.workflows.cancel(run_id)
    if st.output.json_mode:
        st.output.emit_json(r)
    else:
        st.output.success(f"workflow run {r.run_id} is {r.status.value}")


@app.command("nodes-catalog")
@cli_errors
def nodes_catalog(
    ctx: typer.Context,
    kind: Annotated[
        NodeKind | None, typer.Option("--kind", help="test, transform, model, ...")
    ] = None,
    about: Annotated[str | None, typer.Option("--about", help="Show one node's About card")] = None,
    expert: Annotated[bool, typer.Option("--expert")] = False,
) -> None:
    """The node implementations a workflow can use, in plain words."""
    st = state(ctx)
    out = st.output
    with st.client() as api:
        if about is not None:
            impl = api.catalog.workflow_node(about)
            if out.json_mode:
                out.emit_json(impl)
                return
            print_about(out, impl.about, item_id=impl.id, expert=expert)
            out.kv(
                [
                    ("inputs", ", ".join(f"{k}: {v.type.value}" for k, v in impl.inputs.items())),
                    ("outputs", ", ".join(f"{k}: {v.value}" for k, v in impl.outputs.items())),
                    ("runs in", impl.runs_in),
                ]
            )
            return
        items = list(api.catalog.workflow_nodes(kind=kind, limit=200))
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["node", "kind", "what it does"],
        [(n.id, n.kind.value, n.about.summary_plain) for n in items],
    )


# ============================================================ schedules, triggers


@schedules.command("ls")
@cli_errors
def schedules_ls(
    ctx: typer.Context, workflow: Annotated[str, typer.Argument(help="Workflow name or id")]
) -> None:
    """A workflow's schedules."""
    st = state(ctx)
    with st.client() as api:
        items = list(api.workflows.schedules(resolve_workflow(api, workflow).id, limit=200))
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "cron", "timezone", "active", "next run", "last run", "description"],
        [
            (
                s.id,
                s.cron,
                s.timezone,
                "yes" if s.active else "paused",
                _when(s.next_run_at),
                s.last_run_id or "-",
                s.description or "",
            )
            for s in items
        ],
    )


@schedules.command("add")
@cli_errors
def schedules_add(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    cron: Annotated[str, typer.Argument(help='Cron expression, e.g. "0 6 * * 1"')],
    timezone: Annotated[str, typer.Option("--tz", help="IANA time zone")] = "UTC",
    inputs: Annotated[list[str] | None, typer.Option("--input", "-i")] = None,
    params: Annotated[list[str] | None, typer.Option("--param", "-p")] = None,
    revision: Annotated[int | None, typer.Option("--revision", "-r", min=1)] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
) -> None:
    """Run the workflow on a schedule (at most every 15 minutes)."""
    st = state(ctx)
    with st.client() as api:
        s = api.workflows.schedule(
            resolve_workflow(api, workflow).id,
            cron,
            timezone=timezone,
            inputs=parse_assignments(inputs or [], what="--input", json_values=False),
            params=parse_assignments(params or [], what="--param"),
            revision=revision,
            description=description,
        )
    if st.output.json_mode:
        st.output.emit_json(s)
    else:
        st.output.success(f"schedule {s.id}: next run {_when(s.next_run_at)} {s.timezone}")


def _set_active(ctx: typer.Context, workflow: str, schedule_id: str, active: bool) -> None:
    st = state(ctx)
    with st.client() as api:
        s = api.workflows.update_schedule(
            resolve_workflow(api, workflow).id, schedule_id, active=active
        )
    st.output.success(f"schedule {s.id} {'active' if s.active else 'paused'}")


@schedules.command("pause")
@cli_errors
def schedules_pause(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    schedule_id: Annotated[str, typer.Argument(help="Schedule id")],
) -> None:
    """Pause a schedule."""
    _set_active(ctx, workflow, schedule_id, False)


@schedules.command("resume")
@cli_errors
def schedules_resume(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    schedule_id: Annotated[str, typer.Argument(help="Schedule id")],
) -> None:
    """Resume a paused schedule."""
    _set_active(ctx, workflow, schedule_id, True)


@schedules.command("rm")
@cli_errors
def schedules_rm(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    schedule_id: Annotated[str, typer.Argument(help="Schedule id")],
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Delete a schedule."""
    st = state(ctx)
    confirm(st, f"Delete schedule {schedule_id}?", yes=yes)
    with st.client() as api:
        api.workflows.delete_schedule(resolve_workflow(api, workflow).id, schedule_id)
    st.output.success(f"deleted schedule {schedule_id}")


@triggers.command("ls")
@cli_errors
def triggers_ls(
    ctx: typer.Context, workflow: Annotated[str, typer.Argument(help="Workflow name or id")]
) -> None:
    """A workflow's dataset triggers."""
    st = state(ctx)
    with st.client() as api:
        items = list(api.workflows.triggers(resolve_workflow(api, workflow).id, limit=200))
    out = st.output
    if out.json_mode:
        out.emit_json(items)
        return
    out.table(
        ["id", "dataset", "input", "debounce", "active", "last fired"],
        [
            (
                t.id,
                t.dataset,
                t.input,
                f"{t.debounce_seconds}s",
                "yes" if t.active else "no",
                _when(t.last_fired_at),
            )
            for t in items
        ],
    )


@triggers.command("add")
@cli_errors
def triggers_add(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    dataset: Annotated[str, typer.Argument(help="dataset://project/name")],
    input_name: Annotated[
        str, typer.Option("--input", "-i", help="Workflow input to bind")
    ] = "data",
    debounce: Annotated[
        int | None, typer.Option("--debounce", min=0, help="Seconds to wait for more versions")
    ] = None,
    params: Annotated[list[str] | None, typer.Option("--param", "-p")] = None,
) -> None:
    """Run the workflow whenever the dataset gets a new version."""
    st = state(ctx)
    with st.client() as api:
        t = api.workflows.trigger(
            resolve_workflow(api, workflow).id,
            dataset,
            input_name,
            params=parse_assignments(params or [], what="--param"),
            debounce_seconds=debounce,
        )
    if st.output.json_mode:
        st.output.emit_json(t)
    else:
        st.output.success(f"trigger {t.id}: new versions of {t.dataset} run it as {t.input}")


@triggers.command("rm")
@cli_errors
def triggers_rm(
    ctx: typer.Context,
    workflow: Annotated[str, typer.Argument(help="Workflow name or id")],
    trigger_id: Annotated[str, typer.Argument(help="Trigger id")],
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Delete a trigger."""
    st = state(ctx)
    confirm(st, f"Delete trigger {trigger_id}?", yes=yes)
    with st.client() as api:
        api.workflows.delete_trigger(resolve_workflow(api, workflow).id, trigger_id)
    st.output.success(f"deleted trigger {trigger_id}")
