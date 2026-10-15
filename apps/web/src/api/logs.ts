/**
 * Following `GET /v1/runs/{id}/logs` (oldest first, opaque `cursor` = "after this line").
 *
 * The server only returns a `next_cursor` while a page is full, so a tail cannot ask for
 * "after the last line" once it has caught up. Instead it remembers the cursor where the
 * last, still-open page began and re-reads from there on the next poll: at most one partial
 * page is fetched twice, and every older page is fetched exactly once.
 */
import { ApiError } from "./client";
import type { LogLine, Page } from "./types";

export type LogPageFetcher = (cursor: string | null) => Promise<Page<LogLine>>;

export interface LogTail {
  /** Lines kept in view, oldest first; at most `maxLines` (the newest). */
  items: LogLine[];
  /** Where the next poll starts (null = from the first line). */
  resume: string | null;
  /** True when newer pages exist that this poll did not load (the page budget ran out). */
  behind: boolean;
  /** Older lines dropped from the front to respect `maxLines`. */
  dropped: number;
}

export const EMPTY_LOG_TAIL: LogTail = { items: [], resume: null, behind: false, dropped: 0 };

const lineKey = (l: LogLine) => `${l.attempt}:${l.seq}`;

/** Fetch up to `maxPages` pages after `prev.resume` and merge them into a capped buffer. */
export async function advanceLogTail(
  prev: LogTail,
  fetchPage: LogPageFetcher,
  opts: { maxPages: number; maxLines: number },
): Promise<LogTail> {
  const seen = new Set(prev.items.map(lineKey));
  const items = [...prev.items];
  let cursor = prev.resume;
  let behind = false;
  for (let page = 0; page < opts.maxPages; page++) {
    const res = await fetchPage(cursor);
    for (const line of res.items) {
      const key = lineKey(line);
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(line);
    }
    if (!res.next_cursor) break; // caught up: this page stays open, re-read it next time
    cursor = res.next_cursor;
    behind = page === opts.maxPages - 1;
  }
  const excess = Math.max(0, items.length - opts.maxLines);
  return {
    items: excess ? items.slice(excess) : items,
    resume: cursor,
    behind,
    dropped: prev.dropped + excess,
  };
}

/**
 * Poll cadence for a live tail. Every authenticated request counts against the tenant's
 * `requests_per_minute`, shared by all of its members (60/min on the public tier), so a
 * tail fetches one page per tick and never polls faster than {@link LOG_POLL_MS.behind}.
 */
export const LOG_POLL_MS = {
  /** Newer pages exist: fetch the next one (≤ 30 requests/min). */
  behind: 2000,
  /** Caught up: look for new lines (12 requests/min). */
  idle: 5000,
  /** A failure without `Retry-After` (network, 5xx). */
  error: 15_000,
  /** 429 without `Retry-After`: wait out a whole rate window. */
  rateLimited: 60_000,
} as const;

/** Milliseconds until the next poll of a log tail, or `false` to stop polling. */
export function logPollDelay(s: { live: boolean; behind: boolean; error: unknown }): number | false {
  if (!s.live) return false;
  if (s.error instanceof ApiError && (s.error.status === 429 || s.error.status === 503)) {
    if (s.error.retryAfter != null) return Math.max(s.error.retryAfter * 1000, LOG_POLL_MS.behind);
    return s.error.status === 429 ? LOG_POLL_MS.rateLimited : LOG_POLL_MS.error;
  }
  if (s.error) return LOG_POLL_MS.error;
  return s.behind ? LOG_POLL_MS.behind : LOG_POLL_MS.idle;
}
