/**
 * "Learn more" links of the chat panel (v0.20, plan D6): a fixed allow-list from a notice or a
 * glossary word to a page of the user guide. Links never come from an answer: the panel shows
 * a link only for a key in this map, and only when the build names the documentation site
 * (`VITE_DOCS_URL`, an `http(s)` URL; without it the panel shows no links).
 *
 * Paths are relative to the documentation site root and end with `/` (the MkDocs page URLs).
 */
import type { ChatNoticeKind } from "./types";

const ASK = "guides/ask-about-your-dashboard/";

/** The guide page (and section) for each answer notice. */
export const NOTICE_DOCS: Readonly<Record<ChatNoticeKind, string>> = {
  unverified_numbers: `${ASK}#check-the-numbers`,
  tool_limit: `${ASK}#what-the-assistant-can-read`,
  truncated: `${ASK}#what-the-assistant-can-read`,
  budget_low: `${ASK}#daily-limits`,
  transcript_kept: `${ASK}#kept-chats`,
};

/** Other fixed pages the panel may link to. */
export const PAGE_DOCS = {
  about: ASK,
  transcripts: `${ASK}#kept-chats`,
  budget: `${ASK}#daily-limits`,
} as const;

export type DocsKey = ChatNoticeKind | keyof typeof PAGE_DOCS;

function docsBase(): string | null {
  const raw = ((import.meta.env as Record<string, unknown>).VITE_DOCS_URL as string | undefined)?.trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return url.href.endsWith("/") ? url.href : `${url.href}/`;
  } catch {
    return null;
  }
}

const BASE = docsBase();

/** The absolute URL of the guide page for `key`, or `null` (no documentation site configured, or not on the list). */
export function docsHref(key: DocsKey, base: string | null = BASE): string | null {
  if (!base) return null;
  const path = key in NOTICE_DOCS ? NOTICE_DOCS[key as ChatNoticeKind] : (PAGE_DOCS as Record<string, string>)[key];
  return path ? new URL(path, base).href : null;
}
