"""v0.4 commands against a respx-mocked API: endpoints, webhooks, plots, sign-up, tenant,
guided and priors. Destructive commands confirm first; secrets are printed only once, alone
on stdout, by the command that creates them."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mapi_cli.profile import profile_csv
from mapi_spec.api import (
    Endpoint,
    EndpointRevision,
    EndpointSchema,
    GdprExport,
    GuidedDraft,
    GuidedTemplate,
    PlotEmbed,
    PlotEmbedCreated,
    PlotRender,
    PriorTranslation,
    RoleSuggestions,
    Run,
    SignupAccepted,
    SnippetSet,
    TenantDeletion,
    VerificationResult,
    VerificationStatus,
    Webhook,
    WebhookCreated,
    WebhookDelivery,
)
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.storage import crc32c, crc32c_to_base64
from typer.testing import Result

Invoke = Callable[..., Result]
Ex = Callable[..., dict[str, Any]]

URL = "https://api.test"
V1 = f"{URL}/v1"
STORAGE = "https://storage.test"
HEADER = "X-Mapi-Project"
CONN = ("--url", URL, "--token", "tok-secret-value", "--project", "acme/growth")


def page(items: list[Any]) -> dict[str, Any]:
    return {"items": items, "next_cursor": None}


def body_of(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def link(name: str, data: bytes, path: str) -> dict[str, Any]:
    return {
        "name": name,
        "url": f"{STORAGE}/{path}?sig=1",
        "size": len(data),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "generation": 1,
        "content_type": "application/octet-stream",
        "expires_at": "2099-01-01T00:00:00Z",
    }


# ================================================================================= endpoints


def test_endpoints_ls_get_history(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    ep = example(Endpoint)
    route = api.get(f"{V1}/endpoints").respond(200, json=page([ep]))
    res = mapi(*CONN, "endpoints", "ls")
    assert res.exit_code == 0, res.output
    assert "geo-mmm-prod" in res.stdout and "allowed" in res.stdout
    assert route.calls.last.request.headers[HEADER] == "acme/growth"
    api.get(f"{V1}/endpoints/geo-mmm-prod").respond(200, json=ep)
    res = mapi(*CONN, "endpoints", "get", "geo-mmm-prod")
    assert "model://growth/eu-geo-mmm@v3" in res.stdout
    api.get(f"{V1}/endpoints/geo-mmm-prod/history").respond(
        200, json=page([example(EndpointRevision)])
    )
    res = mapi(*CONN, "endpoints", "history", "geo-mmm-prod")
    assert "promote" in res.stdout and "strike" in res.stdout


def test_promote_confirms_and_reports_the_gate(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    ep = example(Endpoint)
    api.get(f"{V1}/endpoints/geo-mmm-prod").respond(200, json=ep)
    put = api.put(f"{V1}/endpoints/geo-mmm-prod").respond(200, json=ep)
    ref = "model://growth/eu-geo-mmm@v4"
    res = mapi(*CONN, "endpoints", "promote", "geo-mmm-prod", ref, input="n\n")
    assert res.exit_code == 1 and not put.called
    assert "now model://growth/eu-geo-mmm@v3" in res.stderr
    res = mapi(*CONN, "endpoints", "promote", "geo-mmm-prod", ref, input="y\n")
    assert res.exit_code == 0, res.output
    assert body_of(put) == {"model": ref, "override": False}
    res = mapi(*CONN, "endpoints", "promote", "geo-mmm-prod", ref, "--override", "--yes")
    assert res.exit_code == 1 and "--reason" in res.stderr
    blocked = Problem.from_code(ErrorCode.OVERRIDE_REQUIRED, "the scorecard warns")
    put.respond(409, json=blocked.model_dump(mode="json"))
    res = mapi(*CONN, "endpoints", "promote", "geo-mmm-prod", ref, "--yes")
    assert res.exit_code == 1 and "OVERRIDE_REQUIRED" in res.stderr
    # a new alias: the 404 of the lookup is not an error
    api.get(f"{V1}/endpoints/new-alias").respond(
        404, json=Problem.from_code(ErrorCode.NOT_FOUND, "no").model_dump(mode="json")
    )
    api.put(f"{V1}/endpoints/new-alias").respond(200, json=ep)
    res = mapi(*CONN, "endpoints", "promote", "new-alias", ref, input="y\n")
    assert res.exit_code == 0 and "(new alias)" in res.stderr


def test_rollback_confirms(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/endpoints/geo-mmm-prod").respond(200, json=example(Endpoint))
    rb = api.post(f"{V1}/endpoints/geo-mmm-prod/rollback").respond(200, json=example(Endpoint))
    res = mapi(*CONN, "endpoints", "rollback", "geo-mmm-prod", "--reason", "bad", input="\n")
    assert res.exit_code == 1 and not rb.called
    res = mapi(
        *CONN, "endpoints", "rollback", "geo-mmm-prod", "--reason", "bad", "--to-revision", "2",
        "-y",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert body_of(rb) == {"reason": "bad", "to_revision": 2}


def test_schema_example_is_plain_json(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    schema = example(EndpointSchema)
    api.get(f"{V1}/endpoints/geo-mmm-prod/schema").respond(200, json=schema)
    res = mapi(*CONN, "endpoints", "schema", "geo-mmm-prod", "--example")
    assert json.loads(res.stdout) == schema["example"]
    res = mapi(*CONN, "endpoints", "schema", "geo-mmm-prod")
    assert "spend_local" in res.stdout and "new_levels" in res.stdout


def test_score_sends_the_example_and_writes_the_tree(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    import xarray as xr
    from mapi_sdk.wire import write_datatree_zip

    tree = xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"representation": "draws"}),
            "/summary": xr.Dataset({"revenue": (("stat",), [1.0])}, coords={"stat": ["mean"]}),
        }
    )
    blob = write_datatree_zip(tree)
    assert blob is not None
    schema = example(EndpointSchema)
    api.get(f"{V1}/endpoints/geo-mmm-prod/schema").respond(200, json=schema)
    run = example(Run, kind="score", state="succeeded", progress=None)
    score = api.post(f"{V1}/endpoints/geo-mmm-prod/score").respond(
        200,
        json={"done": True, "run": run, "outputs": [link("score.zarr.zip", blob, "out.zip")]},
    )
    api.get(f"{STORAGE}/out.zip").respond(200, content=blob)
    dest = tmp_path / "result.zarr.zip"
    res = mapi(
        *CONN, "endpoints", "score", "geo-mmm-prod", "--example", "--periods", "4", "--draws",
        "10", "-o", str(dest),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    sent = body_of(score)
    assert sent["mode"] == "forecast" and sent["horizon"] == {"dim": "time", "periods": 4}
    assert sent["output"]["draws"] == 10 and sent["output"]["summary"] == ["mean", "hdi_90"]
    assert score.calls.last.request.headers["Prefer"] == "wait=60"
    assert dest.read_bytes() == blob
    assert "/summary" in res.stdout and "revenue" in res.stdout


def test_score_inline_xrjson_file(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    payload = tmp_path / "plan.json"
    data = {
        "coords": {"channel": {"dims": ["channel"], "data": ["tv"]}},
        "data_vars": {"spend": {"dims": ["channel"], "data": [10.0]}},
    }
    payload.write_text(json.dumps(data))
    run = example(Run, kind="score", state="succeeded", progress=None)
    score = api.post(f"{V1}/endpoints/geo-mmm-prod/score").respond(
        200, json={"done": True, "run": run, "outputs": []}
    )
    res = mapi(*CONN, "--json", "endpoints", "score", "geo-mmm-prod", "--data", str(payload))
    assert res.exit_code == 0, res.output
    assert body_of(score)["data"]["data_vars"]["spend"]["data"] == [10.0]
    assert json.loads(res.stdout)["run"]["state"] == "succeeded"


def test_snippets_print_code_only_for_one_language(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    snippets = example(SnippetSet)
    snippets["items"][0]["code"] = "curl https://x\x1b[31m\n"  # control codes are stripped
    route = api.get(f"{V1}/endpoints/geo-mmm-prod/snippets").respond(200, json=snippets)
    res = mapi(*CONN, "endpoints", "snippets", "geo-mmm-prod", "-l", "curl")
    assert res.stdout == "curl https://x\n"
    assert route.calls.last.request.url.params.get_list("language") == ["curl"]


# ================================================================================== webhooks


def test_webhook_secret_is_printed_once_alone(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    created = example(WebhookCreated)
    secret = created["secret"]
    post = api.post(f"{V1}/webhooks").respond(201, json=created)
    res = mapi(
        *CONN, "webhooks", "create", "https://hooks.example.com/mapi", "-e", "run.succeeded",
        "-e", "endpoint.promoted",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert res.stdout == secret + "\n"
    assert "not shown again" in res.stderr and secret not in res.stderr
    assert body_of(post)["events"] == ["run.succeeded", "endpoint.promoted"]
    api.get(f"{V1}/webhooks").respond(200, json=page([example(Webhook)]))
    api.get(f"{V1}/webhooks/{created['webhook']['id']}").respond(200, json=example(Webhook))
    for args in (("ls",), ("get", created["webhook"]["id"])):
        res = mapi(*CONN, "webhooks", *args)
        assert res.exit_code == 0 and secret not in res.output
        assert "hooks.example.com" in res.stdout


def test_webhook_rotate_and_rm_confirm(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    wid = example(Webhook)["id"]
    api.get(f"{V1}/webhooks/{wid}").respond(200, json=example(Webhook))
    rot = api.post(f"{V1}/webhooks/{wid}:rotate-secret").respond(200, json=example(WebhookCreated))
    res = mapi(*CONN, "webhooks", "rotate", wid, input="n\n")
    assert res.exit_code == 1 and not rot.called and "whsec_…Q2x9" in res.stderr
    res = mapi(*CONN, "webhooks", "rotate", wid, input="y\n")
    secret = example(WebhookCreated)["secret"]
    # (the test runner echoes the typed answer into stdout; the secret is the last line)
    assert res.exit_code == 0 and res.stdout.splitlines()[-1] == secret
    assert res.output.count(secret) == 1
    rm = api.delete(f"{V1}/webhooks/{wid}").respond(204)
    res = mapi(*CONN, "webhooks", "rm", wid, input="n\n")
    assert res.exit_code == 1 and not rm.called
    res = mapi(*CONN, "webhooks", "rm", wid, "--yes")
    assert res.exit_code == 0 and rm.called


def test_webhook_update_enables_disables_and_edits(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    """A webhook created with --inactive, or disabled automatically after failures, can be
    switched back on from the command line."""
    hook = example(Webhook)
    wid = hook["id"]
    route = api.patch(f"{V1}/webhooks/{wid}").respond(200, json=hook | {"active": True})
    res = mapi(*CONN, "webhooks", "update", wid, "--enable")
    assert res.exit_code == 0, res.output
    assert body_of(route) == {"active": True}
    assert "hooks.example.com" in res.stdout
    res = mapi(*CONN, "webhooks", "update", wid, "--disable")
    assert res.exit_code == 0, res.output
    assert body_of(route) == {"active": False}
    res = mapi(
        *CONN, "webhooks", "update", wid, "-e", "run.failed", "-e", "endpoint.promoted",
        "--description", "on-call", "--url", "https://hooks.example.com/v2",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert body_of(route) == {
        "url": "https://hooks.example.com/v2",
        "events": ["run.failed", "endpoint.promoted"],
        "description": "on-call",
    }
    res = mapi(*CONN, "--json", "webhooks", "update", wid, "--enable")
    assert res.exit_code == 0 and json.loads(res.stdout)["id"] == wid
    calls = route.call_count
    res = mapi(*CONN, "webhooks", "update", wid)
    assert res.exit_code != 0 and route.call_count == calls
    assert "at least one" in res.output


def test_webhook_deliveries_and_replay(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    wid = example(Webhook)["id"]
    hostile = example(WebhookDelivery) | {"error": None, "response_excerpt": "ok\x1b]0;pwned\x07"}
    route = api.get(f"{V1}/webhooks/{wid}/deliveries").respond(200, json=page([hostile]))
    res = mapi(*CONN, "webhooks", "deliveries", wid, "--status", "retrying")
    assert res.exit_code == 0, res.output
    assert "\x1b" not in res.stdout and "retrying" in res.stdout
    assert route.calls.last.request.url.params["status"] == "retrying"
    did = hostile["id"]
    api.post(f"{V1}/webhooks/{wid}/deliveries/{did}:replay").respond(
        202, json=example(WebhookDelivery)
    )
    res = mapi(*CONN, "webhooks", "replay", wid, did)
    assert res.exit_code == 0 and "replay of" in res.stdout


# ===================================================================================== plots


def test_plots_render_downloads_the_file(
    mapi: Invoke,
    api: respx.MockRouter,
    example: Ex,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = b"\x89PNG-bytes"
    render = example(PlotRender) | {
        "format": "png",
        "outputs": [link("renders/mmm.response_curves.png", png, "r.png")],
    }
    post = api.post(f"{V1}/plots:render").respond(200, json=render)
    api.get(f"{STORAGE}/r.png").respond(200, content=png)
    monkeypatch.chdir(tmp_path)
    res = mapi(
        *CONN,
        "plots",
        "render",
        "--model",
        "mdl_1",
        "--version",
        "3",
        "--plot",
        "mmm.response_curves",
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "mmm.response_curves.png").read_bytes() == png
    assert body_of(post)["source"] == {
        "model_id": "mdl_1",
        "version": 3,
        "plot_id": "mmm.response_curves",
    }
    res = mapi(*CONN, "plots", "render", "--saved", "plt_1", "--plot", "x")
    assert res.exit_code == 1 and "exactly one" in res.stderr
    res = mapi(*CONN, "plots", "render", "--saved", "plt_1", "--format", "gif")
    assert res.exit_code == 1


def test_plots_embed_prints_the_link_once(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    created = example(PlotEmbedCreated)
    post = api.post(f"{V1}/plots/plt_1/embeds").respond(201, json=created)
    res = mapi(*CONN, "plots", "embed", "plt_1", "--allow-origin", "https://bi.example")
    assert res.exit_code == 0, res.output
    assert res.stdout == created["url"] + "\n"
    assert body_of(post)["allowed_origins"] == ["https://bi.example"]
    api.get(f"{V1}/plots/plt_1/embeds").respond(200, json=page([example(PlotEmbed)]))
    res = mapi(*CONN, "plots", "embeds", "plt_1")
    assert created["token"] not in res.stdout and "mpe_" in res.stdout
    rv = api.delete(f"{V1}/plots/plt_1/embeds/emb_1").respond(204)
    res = mapi(*CONN, "plots", "revoke", "plt_1", "emb_1", input="n\n")
    assert res.exit_code == 1 and not rv.called
    assert mapi(*CONN, "plots", "revoke", "plt_1", "emb_1", "-y").exit_code == 0


# ==================================================================================== signup


def test_signup_and_verify_send_no_token(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    post = api.post(f"{URL}/auth/signup").respond(202, json=example(SignupAccepted))
    res = mapi(*CONN, "signup", "--email", "ana@acme.example", "--name", "Ana", input="n\n")
    assert res.exit_code == 1 and not post.called
    res = mapi(*CONN, "signup", "--email", "ana@acme.example", "--name", "Ana", "--accept-terms")
    assert res.exit_code == 0, res.output
    assert "authorization" not in post.calls.last.request.headers
    assert "Check ana@acme.example" in res.stdout
    verify = api.post(f"{URL}/auth/verify-email").respond(200, json=example(VerificationResult))
    token = "mev_" + "0123456789abcdefghjkmnpqrstvwxyz01234567"
    res = mapi(*CONN, "verify-email", token)
    assert res.exit_code == 0, res.output
    assert "authorization" not in verify.calls.last.request.headers
    assert "mapi login" in res.stdout


# ==================================================================================== tenant


def test_tenant_status_without_a_deletion(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/tenants/acme/verification").respond(200, json=example(VerificationStatus))
    api.get(f"{V1}/tenants/acme/deletion").respond(
        404, json=Problem.from_code(ErrorCode.NOT_FOUND, "none").model_dump(mode="json")
    )
    res = mapi(*CONN, "tenant", "status")
    assert res.exit_code == 0, res.output
    assert "not allowed" in res.stdout


def test_tenant_delete_needs_the_typed_id(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    post = api.post(f"{V1}/tenants/acme/deletion").respond(202, json=example(TenantDeletion))
    res = mapi(*CONN, "tenant", "delete", input="acme-retail\n")
    assert res.exit_code == 1 and not post.called
    assert "purges every project" in res.stderr
    res = mapi(*CONN, "tenant", "delete", "--confirm", "other")
    assert res.exit_code == 1 and not post.called
    res = mapi(*CONN, "tenant", "delete", input="acme\n")
    assert res.exit_code == 0, res.output
    assert body_of(post) == {"confirm": "acme"}
    assert "Cancel with" in res.stdout
    res = mapi(*CONN, "tenant", "delete", "--confirm", "acme", "--reason", "done")
    assert res.exit_code == 0 and body_of(post)["reason"] == "done"
    cancel = api.delete(f"{V1}/tenants/acme/deletion").respond(200, json=example(TenantDeletion))
    assert mapi(*CONN, "tenant", "delete", "--cancel").exit_code == 0 and cancel.called


def test_tenant_export_downloads_the_archive(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    archive = b"PK\x03\x04archive"
    ready = example(GdprExport) | {
        "tenant_id": "acme",
        "size_bytes": len(archive),
        "download_url": f"{STORAGE}/exp.zip?sig=1",
    }
    post = api.post(f"{V1}/tenants/acme/exports").respond(202, json=ready | {"state": "queued"})
    api.get(f"{V1}/tenants/acme/exports/{ready['id']}").respond(200, json=ready)
    dl = api.get(f"{STORAGE}/exp.zip").respond(200, content=archive)
    dest = tmp_path / "acme.zip"
    res = mapi(*CONN, "tenant", "export", "--include", "metadata", "-o", str(dest))
    assert res.exit_code == 0, res.output
    assert dest.read_bytes() == archive and body_of(post) == {"include": ["metadata"]}
    assert "authorization" not in dl.calls.last.request.headers
    res = mapi(*CONN, "tenant", "export", "--include", "secrets")
    assert res.exit_code == 1 and "unknown --include" in res.stderr


# ==================================================================================== guided


CSV = """week,market,revenue_eur,tv_spend,search_spend,avg_price
2026-01-05,de,1000.5,100,50,9.99
2026-01-12,de,1100.0,120,40,9.99
2026-01-19,de,,90,45,8.99
2026-01-05,fr,900.0,80,20,9.49
"""


def test_profile_csv(tmp_path: Path) -> None:
    path = tmp_path / "media.csv"
    path.write_text(CSV)
    profiles = {p.name: p for p in profile_csv(path)}
    assert profiles["week"].dtype == "date" and profiles["week"].min_date == "2026-01-05"
    assert profiles["market"].dtype == "string" and profiles["market"].n_distinct == 2
    assert profiles["revenue_eur"].dtype == "number" and profiles["revenue_eur"].n_missing == 1
    assert profiles["tv_spend"].dtype == "integer" and profiles["tv_spend"].max == 120
    weekly = tmp_path / "weekly.csv"
    weekly.write_text("week,y\n2026-01-05,1\n2026-01-12,2\n2026-01-19,3\n")
    assert {p.name: p for p in profile_csv(weekly)}["week"].detected_freq == "W-MON"
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1\n")
    with pytest.raises(ValueError, match="line 2"):
        profile_csv(bad)


def test_guided_templates_detect_and_draft(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    api.get(f"{V1}/guided/templates").respond(200, json=page([example(GuidedTemplate)]))
    tpl = example(GuidedTemplate)
    res = mapi(*CONN, "guided", "templates")
    assert res.exit_code == 0 and tpl["id"] in res.stdout
    api.get(f"{V1}/guided/templates/{tpl['id']}").respond(200, json=tpl)
    res = mapi(*CONN, "guided", "templates", tpl["id"])
    assert tpl["questions"][0]["id"] in res.stdout

    csv = tmp_path / "media.csv"
    csv.write_text(CSV)
    detect = api.post(f"{V1}/guided/detect-roles").respond(200, json=example(RoleSuggestions))
    res = mapi(*CONN, "guided", "detect", "geo_mmm", "--csv", str(csv))
    assert res.exit_code == 0, res.output
    sent = body_of(detect)
    assert [c["name"] for c in sent["columns"]][:3] == ["week", "market", "revenue_eur"]
    assert len(json.dumps(sent)) < 4000  # column summaries, not the rows
    assert "revenue_eur" in res.stdout

    draft = api.post(f"{V1}/guided/drafts").respond(200, json=example(GuidedDraft))
    yaml_out = tmp_path / "spec.yaml"
    res = mapi(
        *CONN, "guided", "draft", "geo_mmm", "--name", "eu-geo-mmm", "--dataset", "eu_media",
        "--csv", str(csv), "--detect", "-a", "spend=tv_spend,search_spend", "-a",
        "holdout_periods=8", "--yaml-out", str(yaml_out),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    answers = body_of(draft)["answers"]
    assert answers["kpi"] == "revenue_eur"  # from detection
    assert answers["spend"] == ["tv_spend", "search_spend"] and answers["holdout_periods"] == 8
    assert yaml_out.read_text().startswith("apiVersion: mapi/v1")


def test_priors_preview(mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path) -> None:
    beliefs = tmp_path / "beliefs.yaml"
    beliefs.write_text(
        "channels:\n  tv:\n    roi: [1.5, 4.0]\n    credibility: 0.8\n"
        "  search:\n    roi: [2.0, 6.0]\n"
    )
    done = example(PriorTranslation)
    post = api.post(f"{V1}/guided/prior-translations").respond(200, json=done)
    out = tmp_path / "priors.json"
    res = mapi(
        *CONN, "priors", "preview", "--beliefs", str(beliefs), "--spec", "spc_1", "--revision",
        "2", "-o", str(out),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert body_of(post)["business_priors"]["channels"]["tv"]["roi"] == [1.5, 4.0]
    assert "inside your range" in res.stdout and "tv" in res.stdout
    assert json.loads(out.read_text())["channels"] == ["tv", "search", "social"]
    res = mapi(*CONN, "priors", "preview", "--beliefs", str(beliefs))
    assert res.exit_code == 1 and "--document" in res.stderr


def test_no_command_prints_the_api_token(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/endpoints").respond(200, json=page([example(Endpoint)]))
    api.get(f"{V1}/webhooks").respond(200, json=page([example(Webhook)]))
    for args in (("endpoints", "ls"), ("webhooks", "ls"), ("--json", "webhooks", "ls")):
        res = mapi(*CONN, *args)
        assert "tok-secret-value" not in res.output


def test_http_errors_are_reported(mapi: Invoke, api: respx.MockRouter) -> None:
    api.get(f"{V1}/endpoints/x").mock(return_value=httpx.Response(403, json=Problem.from_code(
        ErrorCode.FORBIDDEN, "no").model_dump(mode="json")))  # fmt: skip
    res = mapi(*CONN, "endpoints", "get", "x")
    assert res.exit_code == 1 and "FORBIDDEN" in res.stderr
