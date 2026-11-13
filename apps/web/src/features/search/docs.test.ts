import { describe, expect, it } from "vitest";

import { docsHref, docsSearchHref, searchDocs, type DocsPage } from "./docs";

const INDEX: DocsPage[] = [
  { t: "Build a client dashboard", s: "Guides", p: "guides/client-dashboards/", h: [["Publish the dashboard", "publish"], ["Give access", "give-access"]] },
  { t: "Dashboards", s: "Concepts", p: "concepts/client-dashboards/", h: [] },
  { t: "Datasets", s: "Guides", p: "guides/datasets/", h: [["Dashboard links", "dashboard-links"]] },
  { t: "Home", s: "Home", p: "", h: [] },
];

describe("searchDocs", () => {
  it("finds pages and headings, a page before a heading with the same score", () => {
    expect(searchDocs(INDEX, "dashboard").map((d) => [d.title, d.secondary, d.path])).toEqual([
      ["Dashboards", "Concepts", "concepts/client-dashboards/"],
      ["Dashboard links", "Guides · Datasets", "guides/datasets/#dashboard-links"],
      ["Build a client dashboard", "Guides", "guides/client-dashboards/"],
      ["Publish the dashboard", "Guides · Build a client dashboard", "guides/client-dashboards/#publish"],
    ]);
  });

  it("marks the match, keeps the limit and refuses loose matches", () => {
    expect(searchDocs(INDEX, "access")[0]).toEqual({ title: "Give access", secondary: "Guides · Build a client dashboard", path: "guides/client-dashboards/#give-access", ranges: [[5, 11]] });
    expect(searchDocs(INDEX, "da", 2)).toHaveLength(2);
    // "dts" is in "Datasets" only as separate letters
    expect(searchDocs(INDEX, "dts")).toEqual([]);
  });
});

describe("the links to the docs", () => {
  it("are under the documentation URL", () => {
    expect(docsHref("https://docs.example.test/", "")).toBe("https://docs.example.test/");
    expect(docsHref("https://docs.example.test/base/", "guides/datasets/#x")).toBe("https://docs.example.test/base/guides/datasets/#x");
    expect(docsSearchHref("https://docs.example.test/", "a b&c")).toBe("https://docs.example.test/?q=a%20b%26c");
  });
});
