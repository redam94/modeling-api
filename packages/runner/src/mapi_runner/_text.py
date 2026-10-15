"""Text hygiene for untrusted worker output: ANSI/control stripping and byte-capped lines."""

from __future__ import annotations

import re

__all__ = ["LineSplitter", "clean_line", "truncate_utf8"]

# CSI (ESC [ ... final), OSC (ESC ] ... BEL | ESC \), DCS/SOS/PM/APC strings, two-byte escapes
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
)
# C0 controls except TAB, DEL, and C1 controls; bidi overrides and isolates (spoofing)
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f\x80-\x9f‪-‮⁦-⁩]")


def clean_line(text: str) -> str:
    """Strip ANSI escape sequences and control characters (TAB becomes a space)."""
    text = _ANSI.sub("", text)
    text = _CONTROL.sub("", text)
    return text.replace("\t", " ")


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``max_bytes`` of UTF-8 (on a character boundary)."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


class LineSplitter:
    """Incremental splitter for a byte stream into lines (``\\n``, ``\\r\\n`` or ``\\r``).

    A line longer than ``max_line_bytes`` is emitted early (flagged as truncated) and the
    rest of it, up to the next line break, is discarded, so memory stays bounded however
    the worker writes.
    """

    def __init__(self, max_line_bytes: int) -> None:
        self.max_line_bytes = max_line_bytes
        self._buf = bytearray()
        self._discarding = False

    def feed(self, data: bytes) -> list[tuple[bytes, bool]]:
        """Consume ``data``; return complete ``(line, truncated)`` pairs."""
        out: list[tuple[bytes, bool]] = []
        start = 0
        n = len(data)
        while start < n:
            nl = _next_break(data, start)
            if nl < 0:
                chunk = data[start:]
                start = n
                if self._discarding:
                    continue
                self._buf += chunk
                if len(self._buf) > self.max_line_bytes:
                    out.append((bytes(self._buf[: self.max_line_bytes]), True))
                    self._buf.clear()
                    self._discarding = True
                continue
            chunk = data[start:nl]
            start = nl + 1
            if self._discarding:
                self._discarding = False
                continue
            self._buf += chunk
            if len(self._buf) > self.max_line_bytes:
                out.append((bytes(self._buf[: self.max_line_bytes]), True))
            else:
                out.append((bytes(self._buf), False))
            self._buf.clear()
        return out

    def flush(self) -> list[tuple[bytes, bool]]:
        """The final unterminated line, if any."""
        if self._discarding or not self._buf:
            self._buf.clear()
            self._discarding = False
            return []
        line = bytes(self._buf)
        self._buf.clear()
        return [(line, False)]


def _next_break(data: bytes, start: int) -> int:
    nl = data.find(b"\n", start)
    cr = data.find(b"\r", start)
    if nl < 0:
        return cr
    if cr < 0:
        return nl
    return min(nl, cr)
