"""The post-filter of chat answers and the grounding check (v0.20, plan D6).

Every answer the model writes goes through :func:`clean_reply` before anyone sees it or it is
signed, stored or counted:

* Unicode NFC; bidirectional controls (U+202A-202E, U+2066-2069, U+200E/U+200F, U+061C),
  zero-width characters and C0/C1 control characters are removed (new lines are kept);
* HTML is removed (comments, ``<script>``/``<style>`` blocks with their content, every tag,
  and the ``<`` of an unclosed tag);
* markdown images become their alt text, markdown links their text, reference definitions
  are removed; bare URLs (``scheme://...``, ``www....``, protocol-relative ``//host/...``,
  bare host names such as ``evil-corp.com/login`` or ``bit.ly/x``) and
  ``javascript:``/``data:``-style targets are removed. URLs are matched on a copy in which
  look-alike colons, slashes and dots (full-width ``：``, ``／``, ``．``, ``。`` and other NFKC
  or confusable forms) are folded to ASCII, so ``https：//evil.com`` is removed too; numbers
  such as ``4.5%`` and versions such as ``v1.2`` stay;
* the result is at most :data:`~mapi_spec.chat.CHAT_MAX_REPLY_CHARS` characters.

The web renders the cleaned text as React text with a tiny markdown subset, so a link or an
image can never come from the model (the "Learn more" links are fixed app strings).

:func:`ungrounded_numbers` lists the numbers of an answer that are not in any tool result of
the turn (after normalising thousands separators, ``%``, ``k``/``m``/``bn`` suffixes and
rounding to the precision shown). A non-empty list adds the notice ``unverified_numbers``
("Check these numbers on the dashboard"). It is a runtime guard, not proof: an answer can
still be wrong with numbers that are all grounded.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterator, Sequence
from typing import Final

from mapi_spec.chat import CHAT_MAX_REPLY_CHARS
from pydantic import JsonValue

__all__ = ["clean_reply", "ungrounded_numbers"]

# ================================================================================== cleaning

_INVISIBLE: Final = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)]  # LRE, RLE, PDF, LRO, RLO
    + [chr(c) for c in range(0x2066, 0x206A)]  # LRI, RLI, FSI, PDI
    + ["\u200e", "\u200f", "\u061c"]  # LRM, RLM, ALM
    + ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u180e", "\u00ad"]  # zero width
    + [chr(c) for c in range(0x2061, 0x2065)]  # invisible operators
)
_NEWLINES: Final = {"\r\n": "\n", "\r": "\n", "\u2028": "\n", "\u2029": "\n\n", "\x85": "\n"}

_HTML_COMMENT: Final = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_HTML_BLOCK: Final = re.compile(
    r"<\s*(script|style|iframe|object|embed|svg|math|template|noscript)\b[^>]*>.*?"
    r"(?:<\s*/\s*\1\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG: Final = re.compile(r"</?[A-Za-z!?][^<>]*>")
_TAG_OPENER: Final = re.compile(r"<+(?=[A-Za-z/!?])")
"""A ``<`` that could start a tag once more text follows (an unclosed ``<script``)."""
_MD_IMAGE: Final = re.compile(r"!\[([^\[\]]*)\](?:\([^()]*(?:\([^()]*\)[^()]*)*\)|\[[^\[\]]*\])?")
_MD_LINK: Final = re.compile(r"\[([^\[\]]*)\](?:\([^()]*(?:\([^()]*\)[^()]*)*\)|\[[^\[\]]*\])")
_MD_REF_DEF: Final = re.compile(r"^[ \t]{0,3}\[[^\[\]]+\]:[ \t]*\S.*$", re.MULTILINE)
_URL: Final = re.compile(r"[A-Za-z0-9+.\-]*://[^\s<>\"'`]*")
_WWW: Final = re.compile(r"www\.[^\s<>\"'`]*", re.IGNORECASE)
_SCRIPT_SCHEME: Final = re.compile(r"(?:java|vb|live)script\s*:[^\s<>\"'`]*", re.IGNORECASE)
_DATA_SCHEME: Final = re.compile(
    r"\b(?:data|file|blob|mailto|tel|ftp|about|filesystem|intent|ms-[a-z\-]+):(?=\S)"
    r"[^\s<>\"'`]*",
    re.IGNORECASE,
)
_PROTOCOL_RELATIVE: Final = re.compile(r"(?<![\w:/])//[A-Za-z0-9][^\s<>\"'`]*")
_COMMON_TLDS: Final = (
    "com|net|org|info|biz|edu|gov|mil|int|io|co|ly|me|ai|app|dev|xyz|top|site|online|link|"
    "click|live|help|support|login|secure|account|page|web|website|cloud|tech|store|shop|"
    "example|test|invalid|localhost|zip|mov|icu|club|vip|win|work|pro|today|news|email"
)
_BARE_HOST: Final = re.compile(
    r"(?<![\w.@\-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:"
    # a letters-only TLD followed by a port, path, query or fragment: always a URL
    r"[A-Za-z]{2,24}(?=(?::\d{1,5})?[/?#])(?::\d{1,5})?[/?#][^\s<>\"'`]*"
    # or a known or two-letter TLD on its own ("evil.com", "login.example.de")
    # (a two-letter TLD only in lower case, so "the readout.So" is not taken for a host)
    rf"|(?:{_COMMON_TLDS}|(?-i:[a-z]{{2}}))(?::\d{{1,5}})?(?![\w\-])"
    r")",
    re.IGNORECASE,
)


def _fold_table() -> dict[int, str]:
    """Full-width ASCII and characters that read as ``:``, ``/`` or ``.`` (NFKC forms and
    common confusables) mapped to ASCII. Every entry is one character to one, so spans
    match the original."""
    # full-width ASCII ("ｗｗｗ．evil．com") reads as ASCII
    table: dict[int, str] = {code: chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)}
    for code in range(0x80, 0x10000):
        folded = unicodedata.normalize("NFKC", chr(code))
        if folded in (":", "/", "."):
            table[code] = folded
    extra = {
        "\u2236": ":", "\ua789": ":", "\u02d0": ":", "\u0589": ":", "\u05c3": ":",
        "\u2215": "/", "\u2044": "/", "\u29f8": "/", "\u2571": "/", "\u3033": "/",
        "\u3002": ".", "\uff61": ".", "\u2024": ".", "\u0701": ".", "\ua4f8": ".",
    }  # fmt: skip
    table.update({ord(k): v for k, v in extra.items()})
    return table


_FOLD: Final = _fold_table()
_URL_PATTERNS: Final = (_URL, _WWW, _PROTOCOL_RELATIVE, _BARE_HOST)


def _strip_urls(text: str) -> str:
    """Remove every URL, matched on a copy with look-alike ``:``/``/``/``.`` folded."""
    folded = text.translate(_FOLD)
    spans = sorted(mt.span() for pat in _URL_PATTERNS for mt in pat.finditer(folded))
    if not spans:
        return text
    out: list[str] = []
    pos = 0
    for start, end in spans:
        if end <= pos:
            continue
        out.append(text[pos : max(pos, start)])
        pos = max(pos, end)
    out.append(text[pos:])
    return "".join(out)


_SPACES: Final = re.compile(r"[ \t\f\v]+")
_BLANK_LINES: Final = re.compile(r"\n{3,}")
_EMPTY_LINK_PARENS: Final = re.compile(r"\(\s*\)")


def _strip_controls(text: str) -> str:
    for old, new in _NEWLINES.items():
        text = text.replace(old, new)
    out: list[str] = []
    for ch in text:
        if ch in _INVISIBLE:
            continue
        if ch == "\n":
            out.append(ch)
            continue
        if ch == "\t":
            out.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc" or cat == "Cs" or cat == "Co" or cat == "Cn":
            continue  # C0/C1 controls, lone surrogates, private use, unassigned
        if cat == "Cf":
            continue  # any other format character (tags, shy, ...)
        out.append(ch)
    return "".join(out)


def _strip_markup(text: str) -> str:
    text = _HTML_COMMENT.sub("", text)
    text = _HTML_BLOCK.sub("", text)
    # images first (their alt text stays), then links (their text stays); repeat for nesting
    while True:
        before = text
        text = _MD_IMAGE.sub(lambda mt: mt.group(1), text)
        text = _MD_LINK.sub(lambda mt: mt.group(1), text)
        if text == before:
            break
    text = _MD_REF_DEF.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = _strip_urls(text)
    text = _SCRIPT_SCHEME.sub("", text)
    text = _DATA_SCHEME.sub("", text)
    text = _EMPTY_LINK_PARENS.sub("", text)
    # "spend < budget" stays; "<script" without its ">" loses the "<"
    return _TAG_OPENER.sub("", text)


def _tidy(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "\u2026"


def clean_reply(text: str) -> str:
    """The answer as people see it: normalised, without controls, HTML, images, links or
    URLs, at most :data:`~mapi_spec.chat.CHAT_MAX_REPLY_CHARS` characters (may be empty)."""
    text = unicodedata.normalize("NFC", text)
    text = _strip_controls(text)
    # markup removal can join pieces into new markup ("<scr<b>ipt>"): repeat until stable
    # (every pass only removes characters, so this ends)
    while True:
        before = text
        text = _strip_markup(text)
        if text == before:
            break
    text = unicodedata.normalize("NFC", _tidy(text))
    return _cap(text, CHAT_MAX_REPLY_CHARS)


# ================================================================================ grounding

_ISO_DATE: Final = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?\b")
_LIST_MARKER: Final = re.compile(r"^(\s*)\d{1,3}[.)](?=\s)", re.MULTILINE)
_REPLY_NUMBER: Final = re.compile(
    r"(?<![\w.,])"
    r"(?P<sign>[-\u2212+]?)"
    r"(?P<cur>[$\u00a3\u20ac\u00a5]\s?)?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(?P<dec>\d+))?"
    r"(?![\w.]*\d)"
    r"(?:\s?(?P<suffix>%|percent\b|pp\b|k\b|K\b|thousand\b|m\b|M\b|mn\b|million\b|bn\b|"
    r"billion\b))?"
)
_PAYLOAD_NUMBER: Final = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_SCALE: Final[dict[str, float]] = {
    "k": 1e3,
    "K": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "M": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "bn": 1e9,
    "billion": 1e9,
}
_SMALL_COUNT: Final = 10
"""Plain whole numbers up to this are counting words ("2 channels"), not results."""
_MAX_PAYLOAD_NUMBERS: Final = 50_000


def _walk(value: JsonValue, out: list[float], dates: list[str]) -> None:
    if len(out) >= _MAX_PAYLOAD_NUMBERS:
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float):
        if math.isfinite(value):
            out.append(float(value))
        return
    if isinstance(value, str):
        dates.append(value)
        for mt in _PAYLOAD_NUMBER.finditer(value):
            try:
                out.append(float(mt.group(0).replace(",", "")))
            except ValueError:  # pragma: no cover - the pattern is numeric
                continue
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _walk(key, out, dates)
            _walk(item, out, dates)
        return
    if isinstance(value, list):
        for item in value:
            _walk(item, out, dates)


def _reply_numbers(text: str) -> Iterator[tuple[str, float, int, str | None]]:
    """``(shown, value, decimals, suffix)`` of every number of the answer that is a result
    (not a list marker, not a small count)."""
    text = _LIST_MARKER.sub(r"\1", text)
    for mt in _REPLY_NUMBER.finditer(text):
        raw_int, dec, suffix = mt.group("int"), mt.group("dec"), mt.group("suffix")
        value = float(raw_int.replace(",", "") + (f".{dec}" if dec else ""))
        if dec is None and suffix is None and mt.group("cur") is None and value <= _SMALL_COUNT:
            continue
        yield mt.group(0).strip(), value, len(dec or ""), suffix


def _matches(value: float, decimals: int, suffix: str | None, candidates: Sequence[float]) -> bool:
    tol = 0.5 * 10.0 ** (-decimals) + 1e-9 * max(1.0, abs(value))
    if suffix in ("%", "percent", "pp"):
        scales: tuple[float, ...] = (1.0, 100.0)
    elif suffix is not None:
        scales = (1.0 / _SCALE[suffix],)
    else:
        scales = (1.0,)
    for c in candidates:
        a = abs(c)
        for scale in scales:
            if abs(a * scale - value) <= tol:
                return True
    return False


def ungrounded_numbers(text: str, payloads: Sequence[JsonValue]) -> list[str]:
    """The numbers of ``text`` (as written) that no tool result of the turn contains, in
    order and without repeats. ISO dates must appear in a result as written."""
    numbers: list[float] = []
    strings: list[str] = []
    for payload in payloads:
        _walk(payload, numbers, strings)
    candidates = sorted(set(numbers))
    out: list[str] = []
    seen: set[str] = set()

    def flag(shown: str) -> None:
        if shown not in seen:
            seen.add(shown)
            out.append(shown)

    def check_date(mt: re.Match[str]) -> str:
        shown = mt.group(0)
        day = shown[:10]
        if not any(day in s for s in strings):
            flag(shown)
        return " "

    rest = _ISO_DATE.sub(check_date, text)
    for shown, value, decimals, suffix in _reply_numbers(rest):
        if not _matches(value, decimals, suffix, candidates):
            flag(shown)
    return out
