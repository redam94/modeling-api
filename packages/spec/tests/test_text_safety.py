"""Untrusted text from uploads (v0.19, T19.19/T19.20): normalization, skeletons, link rules."""

from __future__ import annotations

import unicodedata

import pytest
from mapi_spec.text_safety import (
    BIDI_CONTROLS,
    ZERO_WIDTH,
    check_reference_url,
    has_unsafe_text,
    normalize_untrusted_text,
    skeleton,
)

pytestmark = pytest.mark.unit

# ------------------------------------------------------------------------------ normalization

CORPUS: list[tuple[str, str, str]] = [
    # (case, input, normalized)
    ("plain", "Store mean", "Store mean"),
    ("bidi_override", "Safe ‮gnp.exe", "Safe gnp.exe"),
    ("bidi_isolate", "a⁦b⁩c", "abc"),
    ("bidi_marks", "a‎b‏c؜d", "abcd"),
    ("zero_width_space", "Bay​esian", "Bayesian"),
    ("zero_width_joiners", "a‌b‍c", "abc"),
    ("word_joiner_bom", "﻿a⁠b", "ab"),
    ("soft_hyphen", "co­operate", "cooperate"),
    ("c0_controls", "a\x00b\x07c\x1bd", "abcd"),
    ("c1_controls", "a\x85b\x9bc", "abc"),
    ("del", "a\x7fb", "ab"),
    ("keeps_newline_tab", "line 1\n\tline 2", "line 1\n\tline 2"),
    ("crlf", "a\r\nb\rc", "a\nb\nc"),
    ("nfc_composes", "Café", "Café"),
    ("strips_ends", "  padded  ", "padded"),
    ("removal_then_nfc", "e​́", "é"),
]


@pytest.mark.parametrize(("case", "raw", "expected"), CORPUS, ids=[c[0] for c in CORPUS])
def test_normalize_corpus(case: str, raw: str, expected: str) -> None:
    out = normalize_untrusted_text(raw, max_len=200)
    assert out == expected, case
    assert not has_unsafe_text(out)
    assert unicodedata.is_normalized("NFC", out)
    # idempotent
    assert normalize_untrusted_text(out, max_len=200) == out


def test_unsafe_detection_matches_normalization() -> None:
    for case, raw, expected in CORPUS:
        assert has_unsafe_text(raw) == (raw != expected and raw.strip() != expected), case
    for ch in BIDI_CONTROLS | ZERO_WIDTH:
        assert has_unsafe_text(f"a{ch}b")
    assert has_unsafe_text("Café")  # not NFC
    assert not has_unsafe_text("plain text\nwith a newline\tand a tab")


def test_normalize_caps_the_length() -> None:
    assert normalize_untrusted_text("x" * 600, max_len=500) == "x" * 500
    assert normalize_untrusted_text("abc   def", max_len=4) == "abc"
    assert normalize_untrusted_text("", max_len=10) == ""
    with pytest.raises(ValueError):
        normalize_untrusted_text("a", max_len=-1)


# -------------------------------------------------------------------------------- skeletons


@pytest.mark.parametrize(
    "lookalike",
    [
        "Bayesian MMM",
        "bayesian mmm",
        "Bayesian  M.M.M.",
        "Bаyesian MMM",  # Cyrillic a
        "Bayesіan MMM",  # Cyrillic i
        "Bayesian МММ",  # Cyrillic capital em
        "Ｂａｙｅｓｉａｎ MMM",  # fullwidth
        "Bayes1an MMM",
        "Bayésian MMM",
        "Bayesian rnrnrn",
        "Bayesian​MMM",
    ],
)
def test_lookalikes_share_a_skeleton(lookalike: str) -> None:
    assert skeleton(lookalike) == skeleton("Bayesian MMM")


@pytest.mark.parametrize("other", ["Bayesian MMM with geo", "Store mean", "Weekly totals"])
def test_different_titles_have_different_skeletons(other: str) -> None:
    assert skeleton(other) != skeleton("Bayesian MMM")


def test_skeleton_is_ascii_letters_and_digits() -> None:
    out = skeleton("Ünïcødé — Title №5 ✓")
    assert out.isascii() and out.isalnum()
    assert skeleton("!!!") == ""


# ------------------------------------------------------------------------------------- URLs


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/acme",
        "https://docs.example.co.uk/a/b?c=d#e",
        "https://xn--bcher-kva.example/path",
        "https://example.org:8443/x",
    ],
)
def test_reference_urls_allowed(url: str) -> None:
    assert check_reference_url(url) is None


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("http://example.org/", "https://"),
        ("javascript:alert(1)", "https://"),
        ("data:text/html,<b>x</b>", "https://"),
        ("https://a@b.example/", "user name"),
        ("https://user:pass@example.org/", "user name"),
        ("https://bücher.example/", "ASCII"),
        ("https://exаmple.org/", "ASCII"),  # Cyrillic a: an IDN homograph
        ("https://127.0.0.1/", "IP address"),
        ("https://[::1]/", "IP address"),
        ("https://2130706433/", "IP address"),
        ("https://0x7f.1/", "IP address"),
        ("https://localhost/", "DNS name"),
        ("https:///path", "host"),
        ("https://exa mple.org/", "spaces"),
        ("https://example.org/‮", "hidden"),
        ("https://example.org/" + "a" * 600, "longer"),
        ("https://-bad-.example/", "DNS name"),
    ],
)
def test_reference_urls_refused(url: str, needle: str) -> None:
    reason = check_reference_url(url)
    assert reason is not None, url
    assert needle in reason, reason
