/**
 * Untrusted log text: strip ANSI/VT escape sequences and other control characters so a log
 * line can never restyle the terminal-like viewer or smuggle invisible content. The runner
 * already strips them (plan §8.7); this is defence in depth.
 */

// CSI (ESC [ ... final), OSC (ESC ] ... BEL/ST), DCS/SOS/PM/APC (ESC P|X|^|_ ... ST),
// two-character escapes (ESC + one char), and 8-bit C1 CSI.
const ANSI_RE = new RegExp(
  [
    "\\u001b\\[[0-?]*[ -/]*[@-~]",
    "\\u001b\\][^\\u0007\\u001b]*(?:\\u0007|\\u001b\\\\)?",
    "\\u001b[PX^_][^\\u001b]*(?:\\u001b\\\\)?",
    "\\u001b[@-Z\\\\-_]",
    "\\u009b[0-?]*[ -/]*[@-~]",
  ].join("|"),
  "g",
);

// C0 controls except tab and newline, DEL and C1 controls, and the bidi embedding/override and
// isolate characters that can visually disguise text.
const CONTROL_RE = new RegExp("[\\u0000-\\u0008\\u000b-\\u001f\\u007f-\\u009f\\u202a-\\u202e\\u2066-\\u2069]", "g");

export function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "").replace(/\r\n?/g, "\n").replace(CONTROL_RE, "");
}
