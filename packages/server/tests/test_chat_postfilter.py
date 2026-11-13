"""The chat post-filter and the grounding check (v0.20, plan D6). Pure functions."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_server.services.chat.postfilter import clean_reply, ungrounded_numbers
from mapi_spec.chat import CHAT_MAX_REPLY_CHARS

FIXTURES: Final = Path(__file__).parent / "fixtures" / "chat_injection"
HOSTILE_REPLIES: Final[list[dict[str, Any]]] = json.loads(
    (FIXTURES / "hostile_replies.json").read_text()
)
BIDI: Final = [chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))]
ZERO_WIDTH: Final = ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"]
_MD_LINK: Final = re.compile(r"\[[^\[\]]*\]\([^()]*\)")


# ================================================================================ cleaning


@pytest.mark.parametrize("case", HOSTILE_REPLIES, ids=lambda c: c["text"][:24])
def test_hostile_replies_are_cleaned(case: dict[str, Any]) -> None:
    out = clean_reply(case["text"])
    for bad in case["forbidden"]:
        assert bad not in out, (bad, out)


def test_images_become_their_alt_text_and_links_their_text() -> None:
    assert clean_reply("A ![chart of sales](https://e.example/c.png) B") == "A chart of sales B"
    assert clean_reply("See [the readout](https://e.example/r) now.") == "See the readout now."
    assert clean_reply("Ref [x][1]\n\n[1]: https://e.example") == "Ref x"
    assert clean_reply("<https://e.example/auto>") == ""


def test_controls_bidi_and_zero_width_characters_are_removed() -> None:
    text = "A" + "".join(BIDI) + "B" + "".join(ZERO_WIDTH) + "C\x00\x07\x1b[31m\x85D"
    out = clean_reply(text)
    assert out.startswith("ABC")
    assert not any(ch in out for ch in BIDI + ZERO_WIDTH)
    assert not any(unicodedata.category(ch) == "Cc" and ch != "\n" for ch in out)


def test_plain_text_lists_bold_and_comparisons_are_kept() -> None:
    text = "Two points:\n\n- **Sales** rose by 4%\n- spend < budget and 3 > 2\n\n1. `get_readout`"
    assert clean_reply(text) == text


def test_text_is_nfc_and_capped() -> None:
    assert clean_reply("Cafe\u0301") == "Caf\u00e9"
    out = clean_reply("word " * 2000)
    assert len(out) <= CHAT_MAX_REPLY_CHARS and out.endswith("\u2026")


def test_script_and_data_targets_are_removed() -> None:
    out = clean_reply(
        "x javascript:alert(1) y JavaScript : z vbscript:msgbox data:image/png;base64,AA"
    )
    for bad in ("javascript", "JavaScript :", "vbscript:", "data:image"):
        assert bad not in out
    assert clean_reply("The data: it is good.") == "The data: it is good."


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=400))
def test_clean_reply_never_leaves_markup_or_controls(text: str) -> None:
    out = clean_reply(text)
    assert len(out) <= CHAT_MAX_REPLY_CHARS
    assert "://" not in out
    assert not any(ch in out for ch in BIDI + ZERO_WIDTH)
    assert clean_reply(out) == out  # idempotent


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            ["<", ">", "script", "img", "a", " href=", "!", "[", "]", "(", ")", "https://e.x/",
             "www.e.x", "javascript:", "x", " ", "/", "\u202e", "\u200b"]
        ),
        max_size=60,
    )
)  # fmt: skip
def test_assembled_markup_never_survives(pieces: list[str]) -> None:
    out = clean_reply("".join(pieces))
    assert "://" not in out and "javascript:" not in out.lower() and "www." not in out
    assert "<script" not in out.lower() and "<img" not in out.lower()
    assert not _MD_LINK.search(out)


# =============================================================================== grounding


PAYLOADS: Final = [
    {"seq": 12, "tiles": [{"value": 1234567.0, "delta": 0.042}, {"value": 2.349}]},
    {"lead": "Sales were 3,456 units in week 2026-10-12.", "spend": 15000},
]


@pytest.mark.parametrize(
    "text",
    [
        "Readout 12 shows 1,234,567.",
        "Expected sales were about 1.2 million (1.23m).",
        "Sales rose by 4.2% (4%).",
        "Return on media was 2.35, or 2.3.",
        "Spend was $15,000 ($15k, 15 thousand).",
        "The lead says 3,456 units in the week of 2026-10-12.",
        "There are 2 channels and 3 tiles.",
        "1. First point\n2. Second point",
        "The change was -4.2%.",
    ],
)
def test_grounded_numbers(text: str) -> None:
    assert ungrounded_numbers(text, PAYLOADS) == []


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Readout 13 shows 1,234,567.", ["13"]),
        ("Sales rose by 5.1%.", ["5.1%"]),
        ("Return on media was 2.4 and 2.35.", ["2.4"]),
        ("Expected sales were 1.3 million.", ["1.3 million"]),
        ("On 2026-10-19 sales were 999,999.", ["2026-10-19", "999,999"]),
        ("ROI 42.0 and 42.0 again", ["42.0"]),
    ],
)
def test_ungrounded_numbers(text: str, flagged: list[str]) -> None:
    assert ungrounded_numbers(text, PAYLOADS) == flagged


def test_no_payload_flags_every_result_number() -> None:
    assert ungrounded_numbers("Sales were 1,234 and 12%.", []) == ["1,234", "12%"]
    assert ungrounded_numbers("No numbers here.", []) == []


def test_numbers_versions_and_field_names_survive_url_removal() -> None:
    # chat-security-3: bare host removal must not eat decimals, versions or names
    text = "Sales rose 4.5% in v1.2, e.g. 3.0x; roi.mean is 2.1 and the tv.linear channel."
    assert clean_reply(text) == text
    assert clean_reply("Spend was $1,234.56. Next readout.So it rose.") == (
        "Spend was $1,234.56. Next readout.So it rose."
    )
    assert clean_reply("Please re-login at evil-corp.com/login?u=1 now") == "Please re-login at now"
    assert clean_reply("see //evil.com/x and and/or") == "see and and/or"
