/**
 * Map validation issues (RFC 6901 pointers into the parsed document) to text ranges in the
 * YAML source, so `POST /v1/specs:validate` results land on the right line in Monaco.
 */
import { isMap, isPair, isScalar, isSeq, LineCounter, parseDocument, type Node } from "yaml";

import type { Issue } from "@/api/types";

import { parsePointer } from "./pointer";

/** 1-based, end-exclusive (Monaco convention). */
export interface TextRange {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
}

export interface LocatedIssue {
  issue: Issue;
  range: TextRange;
  /** False if only a prefix of the pointer exists in the source (e.g. a missing key). */
  exact: boolean;
}

export type MarkerSeverity = "error" | "warning" | "info";

export interface Located {
  range: TextRange;
  exact: boolean;
}

/** A parsed YAML document that can resolve many pointers cheaply. */
export class YamlLocator {
  private readonly doc;
  private readonly lines: LineCounter;

  constructor(private readonly text: string) {
    this.lines = new LineCounter();
    this.doc = parseDocument(text, { lineCounter: this.lines, keepSourceTokens: false, uniqueKeys: false });
  }

  locate(pointer: string): Located {
    let segments: string[];
    try {
      segments = parsePointer(pointer);
    } catch {
      return { range: this.firstLine(), exact: false };
    }
    let node: unknown = this.doc.contents;
    let best: [number, number] | null = rangeOf(node);
    let exact = true;
    for (const seg of segments) {
      if (isMap(node)) {
        const pair = node.items.find((p) => isPair(p) && keyText(p.key) === seg);
        if (!pair) {
          exact = false;
          break;
        }
        const keyRange = rangeOf(pair.key);
        const value = pair.value;
        // Point at the key for block collections (their range spans many lines).
        const valueRange = rangeOf(value);
        best = (isMap(value) || isSeq(value)) && keyRange ? keyRange : (valueRange ?? keyRange ?? best);
        node = value;
      } else if (isSeq(node)) {
        const idx = /^\d+$/.test(seg) ? Number(seg) : -1;
        const item: unknown = idx >= 0 ? node.items[idx] : undefined;
        if (item === undefined) {
          exact = false;
          break;
        }
        const r = rangeOf(item);
        if (r) best = isMap(item) || isSeq(item) ? firstLineOf(r, this.text) : r;
        node = item;
      } else {
        exact = false;
        break;
      }
    }
    return { range: best ? this.toRange(best) : this.firstLine(), exact };
  }

  private toRange([start, end]: [number, number]): TextRange {
    const s = this.lines.linePos(start);
    const e = this.lines.linePos(Math.max(start, end));
    // linePos is 1-based for both line and col.
    const endSameOrLater =
      e.line > s.line || (e.line === s.line && e.col > s.col) ? e : { line: s.line, col: s.col + 1 };
    return { startLineNumber: s.line, startColumn: s.col, endLineNumber: endSameOrLater.line, endColumn: endSameOrLater.col };
  }

  private firstLine(): TextRange {
    const nl = this.text.indexOf("\n");
    const len = nl < 0 ? this.text.length : nl;
    return { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: Math.max(2, len + 1) };
  }
}

function keyText(key: unknown): string | null {
  if (!isScalar(key)) return null;
  const v: unknown = key.value;
  if (v === null) return "null";
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" || typeof v === "bigint" ? String(v) : null;
  return null;
}

function rangeOf(node: unknown): [number, number] | null {
  const r = (node as Node | null | undefined)?.range;
  return r ? [r[0], r[1]] : null;
}

/** Trim a multi-line range to its first line (block sequence items, mappings). */
function firstLineOf([start, end]: [number, number], text: string): [number, number] {
  const nl = text.indexOf("\n", start);
  return [start, nl < 0 || nl > end ? end : nl];
}

/** Locate every issue in the YAML text. */
export function locateIssues(text: string, issues: readonly Issue[]): LocatedIssue[] {
  const locator = new YamlLocator(text);
  return issues.map((issue) => {
    const { range, exact } = locator.locate(issue.pointer ?? "");
    return { issue, range, exact };
  });
}

export function severityOf(issue: Issue): MarkerSeverity {
  return issue.severity ?? "error";
}

/** Plain-text marker message: code, message, suggested action. Untrusted text is never shown here. */
export function markerMessage(issue: Issue): string {
  const parts = [`${issue.code}: ${issue.message}`];
  if (issue.suggested_action) parts.push(`Suggested: ${issue.suggested_action}`);
  return parts.join("\n");
}
