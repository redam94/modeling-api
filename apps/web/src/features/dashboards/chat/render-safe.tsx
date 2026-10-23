/**
 * The safe renderer of assistant answers (v0.20, plan D23).
 *
 * An answer is untrusted text: the model wrote it, and it read tenant strings (readout titles,
 * analyst notes, channel names, client actuals) that may hold instructions or markup. The server
 * cleans every answer (`postfilter.clean_reply`); this renderer does not rely on that and makes
 * React elements from a tiny markdown subset only:
 *
 * - paragraphs (a blank line between them; a single line break stays a line break);
 * - `-` / `*` bullet lists and `1.` numbered lists;
 * - `**bold**` and inline code;
 * - numbers in tabular figures (the container sets `tabular-nums`).
 *
 * Everything else is literal text. There are no images, no links, no autolinks, no headings
 * and no raw HTML: `![alt](url)` becomes its alt text and `[label](url)` its label, so no URL
 * from an answer ever reaches an `src` or `href`. Bidirectional controls, zero-width characters
 * and other control characters are removed first, so text cannot hide or reverse what it says.
 * No HTML string is ever made (the app's Trusted Types policy has no `createHTML`).
 */
import type * as React from "react";

import { cn } from "@/lib/utils";

import { CHAT_MAX_REPLY_CHARS } from "./types";

/**
 * Characters an answer never shows: C0 and C1 controls (but tab and line feed), the
 * bidirectional marks, embeddings, overrides and isolates (U+061C, U+200E/F, U+202A-202E,
 * U+2066-2069), and zero-width or invisible characters (U+200B-200D, U+2060-2064, U+FEFF).
 */
const HIDDEN = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u061C\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]/g;
/** A link or image target, with one level of nested brackets (`javascript:alert(x)`). */
const TARGET = String.raw`\((?:[^()\n]|\([^()\n]*\))*\)`;
/** `![alt](target)`: an image becomes its alt text. */
const MD_IMAGE = new RegExp(String.raw`!\[([^\]\n]*)\]` + TARGET, "g");
/** `[label](target)`: a link becomes its label. */
const MD_LINK = new RegExp(String.raw`\[([^\]\n]+)\]` + TARGET, "g");
const BULLET = /^\s{0,3}[-*•]\s+(.*)$/;
const NUMBERED = /^\s{0,3}(\d{1,3})[.)]\s+(.*)$/;
const INLINE = /(\*\*[^*\n]+?\*\*|`[^`\n]+`)/;

/** The text of an answer as the panel shows it: normalised, without hidden characters, images or link targets. */
export function cleanAnswerText(text: string): string {
  return text
    .normalize("NFC")
    .replace(/\r\n?/g, "\n")
    .replace(HIDDEN, "")
    .replace(MD_IMAGE, "$1")
    .replace(MD_LINK, "$1")
    .slice(0, CHAT_MAX_REPLY_CHARS);
}

export type SafeBlock = { kind: "p"; lines: string[] } | { kind: "ul"; items: string[] } | { kind: "ol"; start: number; items: string[] };

/** Split cleaned text into paragraphs and lists (pure; the tests read it directly). */
export function parseBlocks(text: string): SafeBlock[] {
  const blocks: SafeBlock[] = [];
  let cur: SafeBlock | null = null;
  const close = () => {
    if (cur) blocks.push(cur);
    cur = null;
  };
  for (const line of cleanAnswerText(text).split("\n")) {
    if (!line.trim()) {
      close();
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    if (bullet) {
      if (cur?.kind !== "ul") {
        close();
        cur = { kind: "ul", items: [] };
      }
      cur.items.push(bullet[1] ?? "");
    } else if (numbered) {
      if (cur?.kind !== "ol") {
        close();
        cur = { kind: "ol", start: Number(numbered[1]), items: [] };
      }
      cur.items.push(numbered[2] ?? "");
    } else if (cur?.kind === "p") {
      cur.lines.push(line.trim());
    } else {
      close();
      cur = { kind: "p", lines: [line.trim()] };
    }
  }
  close();
  return blocks;
}

/** One line: `**bold**` and inline code as elements, the rest as text. */
function Inline({ text }: { text: string }): React.ReactNode {
  return text.split(INLINE).map((part, i) => {
    if (i % 2 === 0) return part;
    if (part.startsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    return (
      <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]">
        {part.slice(1, -1)}
      </code>
    );
  });
}

/** An assistant answer as React elements (never HTML). */
export function SafeText({ text, className }: { text: string; className?: string }) {
  const blocks = parseBlocks(text);
  return (
    <div className={cn("grid gap-2 break-words tabular-nums", className)}>
      {blocks.map((b, i) =>
        b.kind === "p" ? (
          <p key={i}>
            {b.lines.map((line, j) => (
              <span key={j}>
                {j > 0 ? <br /> : null}
                <Inline text={line} />
              </span>
            ))}
          </p>
        ) : b.kind === "ul" ? (
          <ul key={i} className="grid list-disc gap-1 pl-5">
            {b.items.map((item, j) => (
              <li key={j}>
                <Inline text={item} />
              </li>
            ))}
          </ul>
        ) : (
          <ol key={i} start={b.start} className="grid list-decimal gap-1 pl-5">
            {b.items.map((item, j) => (
              <li key={j}>
                <Inline text={item} />
              </li>
            ))}
          </ol>
        ),
      )}
    </div>
  );
}
