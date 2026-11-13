/**
 * The search reads some lists under the query keys of their features, so the list pages and
 * the search share one cache entry and one invalidation. The keys are written out in
 * `remote-sources.tsx` (an import would move modules between chunks): they must stay equal.
 */
import { describe, expect, it } from "vitest";

import { ik } from "@/api/integrations";
import { aboutKeys } from "@/features/about/api";
import { dk } from "@/features/dashboards/keys";
import { lk } from "@/features/library/keys";
import { wk } from "@/features/workflows/api";

import { searchKeys } from "./remote-sources";

const P = "acme/growth";

describe("the query keys of the search", () => {
  it("are the keys of the list pages", () => {
    expect(searchKeys.dashboards(P)).toEqual(dk.list(P));
    expect(searchKeys.dashboards(P)).toEqual(dk.list(P, {}));
    expect(searchKeys.workflows(P)).toEqual([...wk.workflows(P), { archived: false }]);
    expect(searchKeys.endpoints(P)).toEqual(ik.endpoints(P));
    expect(searchKeys.abouts).toEqual(aboutKeys.abouts);
  });

  it("keeps the library search under the library's lists, apart from the list page's entries", () => {
    const key = searchKeys.library(P, "geo");
    expect(key.slice(0, lk.lists(P).length)).toEqual([...lk.lists(P)]);
    expect(key).not.toEqual(lk.list(P, { q: "geo" }));
  });
});
