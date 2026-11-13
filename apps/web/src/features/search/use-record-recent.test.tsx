import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useScope } from "@/stores/scope";

import { RECENT_LIMIT, useRecents, type RecentItem } from "./recents";
import { useRecordRecent } from "./use-record-recent";

const SCOPE = "acme/growth";
const item = (n: number): RecentItem => ({ group: "runs", title: `Run ${n}`, secondary: `Fit · succeeded · run_${n}`, href: `/runs/run_${n}` });

beforeEach(() => {
  useRecents.setState({ byScope: {} });
  useScope.setState({ active: { tenantId: "acme", projectId: "growth" }, preferred: null, recent: [] });
});

describe("useRecordRecent (v0.21: a visit to a detail page is a recent item)", () => {
  it("records nothing while the page has no data", () => {
    renderHook(() => useRecordRecent(null));
    renderHook(() => useRecordRecent(undefined));
    expect(useRecents.getState().list(SCOPE)).toEqual([]);
  });

  it("records the page in the list of the open project once its data has loaded", () => {
    const { rerender } = renderHook(({ v }: { v: RecentItem | null }) => useRecordRecent(v), { initialProps: { v: null as RecentItem | null } });
    rerender({ v: item(1) });
    expect(useRecents.getState().list(SCOPE)).toEqual([item(1)]);
    expect(useRecents.getState().byScope).toEqual({ [SCOPE]: [item(1)] });
  });

  it("does not write again for a new object with the same content", () => {
    const { rerender } = renderHook(({ v }: { v: RecentItem }) => useRecordRecent(v), { initialProps: { v: item(1) } });
    const before = useRecents.getState().byScope;
    rerender({ v: item(1) });
    expect(useRecents.getState().byScope).toBe(before);
  });

  it("moves a page that is visited again to the top, without a second entry", () => {
    renderHook(() => useRecordRecent(item(1))).unmount();
    renderHook(() => useRecordRecent(item(2))).unmount();
    expect(useRecents.getState().list(SCOPE).map((r) => r.href)).toEqual(["/runs/run_2", "/runs/run_1"]);
    renderHook(() => useRecordRecent({ ...item(1), secondary: "Fit · failed · run_1" })).unmount();
    expect(useRecents.getState().list(SCOPE)).toEqual([{ ...item(1), secondary: "Fit · failed · run_1" }, item(2)]);
  });

  it("keeps the last eight, as the search does", () => {
    for (let i = 1; i <= RECENT_LIMIT + 2; i++) renderHook(() => useRecordRecent(item(i))).unmount();
    const list = useRecents.getState().list(SCOPE);
    expect(list).toHaveLength(RECENT_LIMIT);
    expect(list[0]?.href).toBe(`/runs/run_${RECENT_LIMIT + 2}`);
    expect(list.at(-1)?.href).toBe("/runs/run_3");
  });

  it("keeps the visits of each project apart", () => {
    renderHook(() => useRecordRecent(item(1))).unmount();
    useScope.setState({ active: { tenantId: "acme", projectId: "brand" } });
    renderHook(() => useRecordRecent(item(2))).unmount();
    expect(useRecents.getState().list(SCOPE)).toEqual([item(1)]);
    expect(useRecents.getState().list("acme/brand")).toEqual([item(2)]);
  });

  it("refuses a link that is not a path of the app, as the store does", () => {
    renderHook(() => useRecordRecent({ ...item(1), href: "https://example.com/x" }));
    expect(useRecents.getState().list(SCOPE)).toEqual([]);
  });
});
