"""Untrusted text in uploads (v0.19 WP12; T19.19, T19.20).

``describe`` cases: the control-plane re-check (:func:`mapi_spec.plugins.check_describe`)
refuses them with the named rule at the named pointer. ``render`` cases are stored as plain
text and neutralized where the platform writes them out: never in a platform message, never
in mail, a leading ``'`` in CSV, and plain data in the SBOM.
"""

from __future__ import annotations

import inspect
import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from mapi_spec.plugins import PluginDescribe, check_describe
from mapi_spec.text_safety import has_unsafe_text, normalize_untrusted_text

pytestmark = pytest.mark.unit

builder = sys.modules["mapi_upload_corpus_builder"]
CORPUS = builder.load_corpus()
TEXT_CASES = [c for c in CORPUS.cases if c.group == "text"]
DESCRIBE_STAGE = [c for c in TEXT_CASES if c.stage == "describe"]


def describe_of(corpus: Any, name: str) -> PluginDescribe:
    case = corpus.by_name()[name]
    return PluginDescribe.model_validate(builder.describe_doc(corpus, case))


def issues(corpus: Any, d: PluginDescribe) -> list[Any]:
    return check_describe(d, namespace=corpus.namespace, builtin_titles=corpus.builtin_titles)


def hostile(name: str) -> str:
    value = builder.TEXT_VALUES[name][1]
    assert isinstance(value, str)
    return value


def test_the_clean_describe_has_no_issues(corpus: Any) -> None:
    d = PluginDescribe.model_validate(builder.base_describe(corpus))
    assert issues(corpus, d) == []


def test_every_text_case_has_a_builder(corpus: Any) -> None:
    for case in TEXT_CASES:
        if case.artifact == "describe":
            assert case.name in builder.TEXT_VALUES, case.name


@pytest.mark.parametrize("case", DESCRIBE_STAGE, ids=lambda c: c.name)
def test_check_describe_refuses_the_case(case: Any, corpus: Any) -> None:
    found = issues(corpus, describe_of(corpus, case.name))
    rules = [(i.context.get("check"), i.pointer) for i in found]
    hits = [i for i in found if i.context.get("check") == case.expect]
    assert hits, (case.name, rules)
    assert any(i.pointer == case.path for i in hits), (case.name, case.path, rules)
    for i in found:
        # the uploaded text is never in the platform's message; only in untrusted_text,
        # and normalized there
        assert "‮" not in i.message and "​" not in i.message
        assert "evil.example" not in i.message
        if i.untrusted_text is not None:
            assert not has_unsafe_text(i.untrusted_text)


def test_title_script_tag_is_stored_as_text(corpus: Any) -> None:
    title = hostile("title_script_tag")
    d = describe_of(corpus, "title_script_tag")
    # not refused: markup is text, the web renders it as text (never innerHTML)
    assert issues(corpus, d) == []
    stored = d.exports.steps[0].about
    assert stored is not None and stored.title == title
    assert normalize_untrusted_text(title, max_len=200) == title  # kept verbatim, as data
    assert json.loads(d.model_dump_json())["exports"]["steps"][0]["about"]["title"] == title


def test_csv_formula_title_is_neutralized_in_csv(corpus: Any) -> None:
    from mapi_server.services.dashboards.downloads import csv_cell

    title = hostile("csv_formula_title")
    d = describe_of(corpus, "csv_formula_title")
    assert issues(corpus, d) == []
    assert csv_cell(title) == "'" + title
    for lead in ("=", "+", "-", "@", "\t", "\r"):
        assert csv_cell(lead + "1+1 x").startswith("'")


def test_mail_never_carries_uploaded_text(corpus: Any) -> None:
    from mapi_server.services.plugins import notify

    # the mail builder takes no uploaded text at all
    params = set(inspect.signature(notify._mail).parameters)
    assert params == {"to", "event", "version", "namespace", "web_origin"}
    version = SimpleNamespace(
        id=corpus.plugin_version_id,
        plugin_id="plg_" + "0" * 26,
        version=corpus.version,
        state="kit_passed",
        upload_sha256="sha256:" + "ab" * 32,
    )
    title = hostile("mail_html_title")
    for event in notify.PLUGIN_EVENTS:
        msg = notify._mail(
            "admin@acme.test", event, version, corpus.namespace, "http://127.0.0.1:15173"
        )
        text = msg.subject + "\n" + msg.body
        assert title not in text and "<" not in text and ">" not in text
        assert not hasattr(msg, "html")


def test_sbom_keeps_an_html_file_name_as_plain_data(corpus: Any, build: Any) -> None:
    art = build("sbom_name_html")
    insp = builder.inspect_artifact(corpus, art)
    name = builder.sbom_html_name(corpus)
    assert insp.ok, [(p.check, p.path, p.message) for p in insp.problems]
    assert insp.sbom is not None
    assert name in {f.path for f in insp.sbom.files}
    raw = json.loads(insp.model_dump_json())
    assert name in {f["path"] for f in raw["sbom"]["files"]}  # a JSON string, nothing more
