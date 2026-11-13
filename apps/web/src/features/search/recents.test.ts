import { beforeEach, describe, expect, it } from "vitest";

import { RECENT_LIMIT, useRecents, type RecentItem } from "./recents";

const item = (n: number, over: Partial<RecentItem> = {}): RecentItem => ({ group: "datasets", title: `Dataset ${n}`, secondary: `ds_${n}`, href: `/datasets/ds_${n}`, ...over });
const titles = (scope: string) => useRecents.getState().list(scope).map((r) => r.title);

beforeEach(() => {
  useRecents.setState({ byScope: {} });
});

describe("recent items of the search", () => {
  it("puts the newest first and does not repeat an item", () => {
    const { add } = useRecents.getState();
    add("acme/growth", item(1));
    add("acme/growth", item(2));
    expect(titles("acme/growth")).toEqual(["Dataset 2", "Dataset 1"]);
    add("acme/growth", item(1, { title: "Dataset 1 (renamed)" }));
    expect(titles("acme/growth")).toEqual(["Dataset 1 (renamed)", "Dataset 2"]);
  });

  it(`keeps the last ${RECENT_LIMIT}`, () => {
    expect(RECENT_LIMIT).toBe(8);
    for (let n = 1; n <= 11; n++) useRecents.getState().add("acme/growth", item(n));
    expect(titles("acme/growth")).toEqual([11, 10, 9, 8, 7, 6, 5, 4].map((n) => `Dataset ${n}`));
  });

  it("keeps one list for each project", () => {
    useRecents.getState().add("acme/growth", item(1));
    useRecents.getState().add("acme/retail", item(2));
    expect(titles("acme/growth")).toEqual(["Dataset 1"]);
    expect(titles("acme/retail")).toEqual(["Dataset 2"]);
    expect(titles("globex/sandbox")).toEqual([]);
  });

  it("clears the list of one project only", () => {
    useRecents.getState().add("acme/growth", item(1));
    useRecents.getState().add("acme/retail", item(2));
    useRecents.getState().clear("acme/growth");
    expect(titles("acme/growth")).toEqual([]);
    expect(titles("acme/retail")).toEqual(["Dataset 2"]);
  });

  it("stores the lists in localStorage", () => {
    useRecents.getState().add("acme/growth", item(1));
    const stored = JSON.parse(localStorage.getItem("mapi.search.recent") ?? "{}") as { state?: { byScope?: Record<string, RecentItem[]> } };
    expect(stored.state?.byScope?.["acme/growth"]?.[0]?.href).toBe("/datasets/ds_1");
  });

  it("drops stored values that are not well-formed in-app links", async () => {
    localStorage.setItem(
      "mapi.search.recent",
      JSON.stringify({
        version: 1,
        state: {
          byScope: {
            "acme/growth": [item(1), { ...item(2), href: "//evil.example/x" }, { ...item(3), href: "https://evil.example/" }, { ...item(4), group: "nonsense" }, { title: 5 }, "text", item(6)],
            "not a scope": [item(7)],
            "acme/retail": "text",
          },
        },
      }),
    );
    await useRecents.persist.rehydrate();
    expect(titles("acme/growth")).toEqual(["Dataset 1", "Dataset 6"]);
    expect(Object.keys(useRecents.getState().byScope)).toEqual(["acme/growth"]);
  });
});
