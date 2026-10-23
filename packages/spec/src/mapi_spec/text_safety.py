"""Untrusted text from uploaded code: normalization, confusable skeletons and URL rules (v0.19).

Every string an uploaded plugin writes (titles, About text, descriptions, kit messages,
diagnostics, file names) and every reviewer note is *untrusted text*. The control plane
normalizes it before it stores or shows it, and the web app renders it as text only:

* :func:`normalize_untrusted_text` - NFC, strip bidirectional controls, zero-width characters
  and C0/C1 controls (keeps ``\\n`` and ``\\t``), then cap the length;
* :func:`has_unsafe_text` - true when :func:`normalize_untrusted_text` would remove something;
* :func:`skeleton` - a confusable skeleton (ASCII letters and digits) used to refuse uploaded
  titles that look like a built-in title (``title_confusable``);
* :func:`check_reference_url` - the rule for links in an uploaded About card: ``https://``
  only, no ``user:pass@``, no IP literal, an ASCII (punycode) host.

Stdlib only (``unicodedata``, ``urllib.parse``, ``ipaddress``): the control plane, the runner
and the CLI share it.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "BIDI_CONTROLS",
    "REFERENCE_URL_MAX",
    "ZERO_WIDTH",
    "check_reference_url",
    "has_unsafe_text",
    "normalize_untrusted_text",
    "skeleton",
]

BIDI_CONTROLS: Final[frozenset[str]] = frozenset(
    chr(c)
    for c in (
        *range(0x202A, 0x202F),  # LRE, RLE, PDF, LRO, RLO
        *range(0x2066, 0x206A),  # LRI, RLI, FSI, PDI
        0x200E,  # LRM
        0x200F,  # RLM
        0x061C,  # ARABIC LETTER MARK
    )
)
"""Bidirectional formatting characters (they can make text read differently than it is)."""

ZERO_WIDTH: Final[frozenset[str]] = frozenset(
    chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E, 0x00AD)
)
"""Invisible characters (zero-width space/joiners, word joiner, BOM, soft hyphen)."""

_KEEP: Final = frozenset("\n\t")

REFERENCE_URL_MAX: Final = 500
"""Longest About reference URL (the :class:`~mapi_spec.about.AboutReference` cap)."""


def _drop(ch: str) -> bool:
    if ch in _KEEP:
        return False
    if ch in BIDI_CONTROLS or ch in ZERO_WIDTH:
        return True
    code = ord(ch)
    # C0 controls, DEL and C1 controls
    return code < 0x20 or 0x7F <= code <= 0x9F


def normalize_untrusted_text(s: str, *, max_len: int) -> str:
    """``s`` in NFC without bidi controls, zero-width characters or C0/C1 controls.

    ``\\r\\n`` and ``\\r`` become ``\\n``; ``\\n`` and ``\\t`` are kept. Leading and trailing
    whitespace is removed, and the result is cut to ``max_len`` characters (never in the middle
    of a surrogate pair: Python strings hold code points).
    """
    if max_len < 0:
        raise ValueError("max_len must not be negative")
    text = unicodedata.normalize("NFC", s.replace("\r\n", "\n").replace("\r", "\n"))
    cleaned = "".join(ch for ch in text if not _drop(ch)).strip()
    # removing characters can leave a sequence that NFC composes differently
    cleaned = unicodedata.normalize("NFC", cleaned)
    return cleaned[:max_len].rstrip() if len(cleaned) > max_len else cleaned


def has_unsafe_text(s: str) -> bool:
    """True if ``s`` holds a character :func:`normalize_untrusted_text` removes, or is not NFC."""
    if any(_drop(ch) for ch in s):
        return True
    return unicodedata.normalize("NFC", s) != s


# Characters that look like ASCII letters or digits, folded to the letter they look like.
# Not the full Unicode confusables table: the common Latin look-alikes from Cyrillic, Greek,
# Armenian and the mathematical/fullwidth forms (the latter are already folded by NFKC).
# fmt: off
_CONFUSABLES: Final[dict[str, str]] = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "һ": "h", "і": "i", "ї": "i", "ј": "j",
    "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l", "ɡ": "g", "ь": "b",
    "п": "n", "г": "r", "з": "3", "ч": "4", "б": "6",
    # Greek
    "α": "a", "β": "b", "γ": "y", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v",
    "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w", "ϲ": "c", "ϳ": "j",
    "μ": "u", "σ": "o",
    # Armenian and others
    "օ": "o", "ս": "u", "ց": "g", "հ": "h", "ո": "n", "ա": "w", "ı": "i", "ȷ": "j",
    "ℓ": "l", "ǀ": "l", "ꓲ": "l", "ꓳ": "o", "ꓴ": "u",
}
# fmt: on
# then the ASCII look-alikes, so "B4yes1an" and "Bayesian" share one skeleton
_ASCII_FOLD: Final = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "5": "s", "4": "a"})
_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")


def skeleton(s: str) -> str:
    """A confusable skeleton of ``s``: lowercase ASCII letters and digits only.

    Steps: NFKC (folds fullwidth and mathematical letters), casefold, NFKD and drop combining
    marks (``é`` -> ``e``), map known look-alike letters (Cyrillic ``а`` -> ``a``), fold ASCII
    look-alikes (``0`` -> ``o``, ``1``/``i``/``|`` -> ``l``, ``rn`` -> ``m``, ``vv`` -> ``w``) and
    remove everything that is not a letter or a digit. Two titles with the same skeleton look
    alike to a reader.
    """
    text = unicodedata.normalize("NFKC", s).casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(_CONFUSABLES.get(ch, ch) for ch in text)
    text = _NON_ALNUM.sub("", text.translate(_ASCII_FOLD))
    return text.replace("rn", "m").replace("vv", "w")


def _ip_literal(host: str) -> bool:
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return True
    # forms the URL parser and browsers accept as IPv4 (decimal, hex or octal parts)
    return bool(re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}", candidate))


_HOST_LABEL: Final = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def check_reference_url(url: str) -> str | None:
    """Why ``url`` may not be a link in an uploaded About card (plain text), else ``None``.

    Allowed: ``https://`` with a DNS host name written in ASCII (punycode ``xn--`` labels are
    fine; the web app shows the host next to the link). Refused: other schemes, ``user@`` or
    ``user:pass@`` before the host, an IP address, a non-ASCII host, whitespace or control
    characters, and URLs longer than :data:`REFERENCE_URL_MAX` characters.
    """
    if len(url) > REFERENCE_URL_MAX:
        return f"The link is longer than {REFERENCE_URL_MAX} characters. Use a shorter link."
    if has_unsafe_text(url) or any(ch.isspace() for ch in url):
        return "The link contains spaces or hidden characters. Remove them."
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "The link is not a valid URL."
    del port
    if parts.scheme.lower() != "https":
        return "Links must start with https://."
    if "@" in parts.netloc:
        return "Links must not contain a user name or password before the host."
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return "The link has no host name."
    if not host.isascii():
        return "Write the host name in ASCII (punycode, xn--), not in other scripts."
    if _ip_literal(host):
        return "Links must use a host name, not an IP address."
    labels = host.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
        return "The link's host name is not a valid DNS name."
    return None
