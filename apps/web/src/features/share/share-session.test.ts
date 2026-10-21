import { describe, expect, it, vi } from "vitest";

import { ShareApiError } from "@/features/dashboards/client";
import type { ClientDashboard, ShareSession } from "@/features/dashboards/types";

import { createShareSession, EXPIRY_MARGIN_MS, refreshDelay } from "./share-session";

const T0 = Date.parse("2026-10-20T09:00:00Z");
const dashboard = { id: "dsh_1", title: "Weekly" } as ClientDashboard;
const sessionAt = (name: string, from: number, ttlS = 1800): ShareSession => ({ session: name, expires_at: new Date(from + ttlS * 1000).toISOString(), dashboard });

function harness(refresh: (s: string) => Promise<ShareSession>) {
  let now = T0;
  const timers: { fn: () => void; ms: number; id: number }[] = [];
  let next = 1;
  const onEnded = vi.fn();
  const onDashboard = vi.fn();
  const c = createShareSession(sessionAt("mps_a", T0), {
    refresh,
    now: () => now,
    onEnded,
    onDashboard,
    setTimer: (fn, ms) => {
      const id = next++;
      timers.push({ fn, ms, id });
      return id;
    },
    clearTimer: (h) => {
      const i = timers.findIndex((t) => t.id === h);
      if (i >= 0) timers.splice(i, 1);
    },
  });
  return { c, timers, onEnded, onDashboard, setNow: (t: number) => (now = t) };
}

describe("refreshDelay", () => {
  it("is 80 % of the lifetime", () => {
    expect(refreshDelay(T0, T0 + 1_800_000)).toBe(1_440_000);
    expect(refreshDelay(T0, T0 - 5)).toBe(0);
  });
});

describe("createShareSession", () => {
  it("schedules a refresh at 80 % of the TTL and swaps in the new session", async () => {
    const refresh = vi.fn(async (s: string) => {
      expect(s).toBe("mps_a");
      return sessionAt("mps_b", T0 + 1_440_000);
    });
    const h = harness(refresh);
    expect(h.timers).toHaveLength(1);
    expect(h.timers[0]?.ms).toBe(1_440_000);
    expect(h.c.handle.current()).toBe("mps_a");
    h.setNow(T0 + 1_440_000);
    h.timers.shift()!.fn(); // a timer that fired is gone
    await vi.waitFor(() => expect(h.c.handle.current()).toBe("mps_b"));
    expect(h.onDashboard).toHaveBeenCalledWith(dashboard);
    expect(h.timers).toHaveLength(1); // the next refresh
  });

  it("reports expiry a little early and shares one refresh between callers", async () => {
    let resolve: (s: ShareSession) => void = () => undefined;
    const refresh = vi.fn(() => new Promise<ShareSession>((r) => (resolve = r)));
    const h = harness(refresh);
    expect(h.c.handle.expired()).toBe(false);
    h.setNow(T0 + 1_800_000 - EXPIRY_MARGIN_MS);
    expect(h.c.handle.expired()).toBe(true);
    const a = h.c.handle.refresh();
    const b = h.c.handle.refresh();
    expect(refresh).toHaveBeenCalledTimes(1);
    resolve(sessionAt("mps_c", T0 + 1_800_000));
    await Promise.all([a, b]);
    expect(h.c.handle.current()).toBe("mps_c");
    expect(h.c.handle.expired()).toBe(false);
  });

  it("ends on 410 and stops refreshing", async () => {
    const h = harness(async () => {
      throw new ShareApiError(410, { type: "about:blank", code: "LINK_EXPIRED", title: "The link has expired or was removed", status: 410 });
    });
    await expect(h.c.handle.refresh()).rejects.toBeInstanceOf(ShareApiError);
    expect(h.onEnded).toHaveBeenCalledTimes(1);
    expect(h.timers).toHaveLength(0);
    await expect(h.c.handle.refresh()).rejects.toThrow(/closed/);
  });

  it("keeps the session after a failure that may pass", async () => {
    const h = harness(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(h.c.handle.refresh()).rejects.toBeInstanceOf(TypeError);
    expect(h.onEnded).not.toHaveBeenCalled();
    expect(h.c.handle.current()).toBe("mps_a");
  });

  it("stop() clears the timer", () => {
    const h = harness(async () => sessionAt("x", T0));
    h.c.stop();
    expect(h.timers).toHaveLength(0);
  });
});
