import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import { advanceLogTail, EMPTY_LOG_TAIL, LOG_POLL_MS, logPollDelay, type LogPageFetcher } from "./logs";
import type { LogLine, Page } from "./types";

/** A server like `GET /v1/runs/{id}/logs`: oldest first, opaque cursor after the last line. */
function fakeServer() {
  const lines: LogLine[] = [];
  const requests: (string | null)[] = [];
  const append = (n: number) => {
    for (let i = 0; i < n; i++) {
      const seq = lines.length + 1;
      lines.push({ run_id: "run_1", attempt: 1, seq, ts: "2026-10-15T09:30:00Z", stream: "stdout", line: `line ${seq}` });
    }
  };
  const fetchPage: LogPageFetcher = async (cursor) => {
    requests.push(cursor);
    const start = cursor ? Number(atob(cursor)) : 0;
    const items = lines.slice(start, start + 200);
    const more = start + 200 < lines.length;
    return { items, next_cursor: more ? btoa(String(start + 200)) : null } satisfies Page<LogLine>;
  };
  return { append, fetchPage, requests };
}

const seqs = (items: LogLine[]) => items.map((l) => l.seq);

describe("advanceLogTail", () => {
  it("keeps following a live run past the first pages instead of re-reading the start", async () => {
    const srv = fakeServer();
    srv.append(2500);
    let tail = await advanceLogTail(EMPTY_LOG_TAIL, srv.fetchPage, { maxPages: 10, maxLines: 10_000 });
    expect(tail.items).toHaveLength(2000);
    expect(tail.behind).toBe(true); // newer lines exist and are not loaded yet
    expect(srv.requests).toHaveLength(10);

    srv.requests.length = 0;
    tail = await advanceLogTail(tail, srv.fetchPage, { maxPages: 10, maxLines: 10_000 });
    expect(tail.items).toHaveLength(2500);
    expect(tail.items.at(-1)?.seq).toBe(2500);
    expect(tail.behind).toBe(false);
    expect(srv.requests[0]).not.toBeNull(); // resumed, not restarted
    expect(srv.requests).toHaveLength(3); // pages 2000-2199, 2200-2399, 2400-2499

    // The run keeps logging: one poll fetches only the open last page onward, without duplicates.
    srv.append(3);
    srv.requests.length = 0;
    tail = await advanceLogTail(tail, srv.fetchPage, { maxPages: 10, maxLines: 10_000 });
    expect(srv.requests).toHaveLength(1);
    expect(tail.items).toHaveLength(2503);
    expect(seqs(tail.items).slice(-4)).toEqual([2500, 2501, 2502, 2503]);
    expect(new Set(seqs(tail.items)).size).toBe(2503);
  });

  it("caps the buffer to the newest lines and counts what it dropped", async () => {
    const srv = fakeServer();
    srv.append(900);
    const tail = await advanceLogTail(EMPTY_LOG_TAIL, srv.fetchPage, { maxPages: 10, maxLines: 500 });
    expect(tail.items).toHaveLength(500);
    expect(tail.items[0]?.seq).toBe(401);
    expect(tail.items.at(-1)?.seq).toBe(900);
    expect(tail.dropped).toBe(400);
    expect(tail.behind).toBe(false);
  });

  it("does nothing new when the log has not grown", async () => {
    const srv = fakeServer();
    srv.append(150);
    const first = await advanceLogTail(EMPTY_LOG_TAIL, srv.fetchPage, { maxPages: 10, maxLines: 10_000 });
    const again = await advanceLogTail(first, srv.fetchPage, { maxPages: 10, maxLines: 10_000 });
    expect(seqs(again.items)).toEqual(seqs(first.items));
    expect(srv.requests).toEqual([null, null]); // the only page is still open: re-read from its start
  });
});

describe("logPollDelay", () => {
  const PUBLIC_TIER_RPM = 60; // the smallest tier's requests_per_minute (mapi_spec.quotas)

  it("does not poll a finished run", () => {
    expect(logPollDelay({ live: false, behind: true, error: null })).toBe(false);
  });

  it("catches up one page every couple of seconds, not in a burst", () => {
    const behind = logPollDelay({ live: true, behind: true, error: null });
    expect(behind).toBeGreaterThanOrEqual(2000);
    // One page per tick: a catching-up tail alone stays well inside the public tier's budget.
    expect(60_000 / Number(behind)).toBeLessThanOrEqual(PUBLIC_TIER_RPM / 2);
    const idle = logPollDelay({ live: true, behind: false, error: null });
    expect(idle).toBeGreaterThanOrEqual(Number(behind));
  });

  it("waits out Retry-After on 429 RATE_LIMITED", () => {
    const err = new ApiError(429, null, "rate limited", 37);
    expect(logPollDelay({ live: true, behind: true, error: err })).toBe(37_000);
    // Never sooner than the normal cadence, even for a tiny Retry-After.
    expect(logPollDelay({ live: true, behind: true, error: new ApiError(429, null, "x", 0) })).toBe(LOG_POLL_MS.behind);
    // No header: back off for a whole rate window.
    expect(logPollDelay({ live: true, behind: true, error: new ApiError(429, null, "x") })).toBe(60_000);
  });

  it("backs off on other failures instead of hammering", () => {
    expect(logPollDelay({ live: true, behind: true, error: new ApiError(503, null, "x", 5) })).toBe(5_000);
    expect(logPollDelay({ live: true, behind: true, error: new TypeError("network") })).toBe(LOG_POLL_MS.error);
  });
});
