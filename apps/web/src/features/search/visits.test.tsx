/**
 * v0.21: each detail page records its visit for the search's Recent group
 * (`use-record-recent.ts`), with the link the search itself uses for the thing.
 */
import { waitFor } from "@testing-library/react";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import * as fx from "@/mocks/fixtures";
import { createHandlers } from "@/mocks/handlers";
import { ENDPOINT_ALIAS } from "@/mocks/integrations";
import { renderApp } from "@/test/render-app";

import { useRecents } from "./recents";

const server = setupServer(...createHandlers());

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  localStorage.clear();
  useRecents.setState({ byScope: {} });
  server.resetHandlers(...createHandlers());
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const DASHBOARD = "dsh_01jaf3w1mmm2weekly3x4y5z6";
const WORKFLOW = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0";

/** [the page, the group of the recent item, its link, its title]. */
const PAGES: [string, string, string, string][] = [
  // A run shows its name, or its id when it has no name.
  [`/runs/${fx.RUN_DONE}`, "runs", `/runs/${fx.RUN_DONE}`, ""],
  [`/datasets/${fx.DS_ID}`, "datasets", `/datasets/${fx.DS_ID}`, "eu_media_daily"],
  [`/build/${fx.SPEC_ID}`, "specs", `/build/${fx.SPEC_ID}`, "eu-geo-mmm"],
  [`/models/${fx.MODEL_ID}`, "models", `/models/${fx.MODEL_ID}`, "eu-geo-mmm"],
  [`/models/${fx.MODEL_ID}/versions/1?tab=diagnostics`, "models", `/models/${fx.MODEL_ID}/versions/1`, "eu-geo-mmm v1"],
  [`/experiments/${fx.EXP_GEO}`, "experiments", `/experiments/${fx.EXP_GEO}`, "Geo adstock study"],
  [`/dashboards/${DASHBOARD}?tab=access`, "dashboards", `/dashboards/${DASHBOARD}`, "Weekly media readout"],
  [`/workflows/${WORKFLOW}?tab=runs`, "workflows", `/workflows/${WORKFLOW}`, "weekly-readiness"],
  [`/integrations/endpoints/${ENDPOINT_ALIAS}`, "endpoints", `/integrations/endpoints/${ENDPOINT_ALIAS}`, ENDPOINT_ALIAS],
];

describe("detail pages record their visit", () => {
  it.each(PAGES)("%s", async (url, group, href, title) => {
    renderApp(url);
    await waitFor(
      () => {
        const lists = Object.values(useRecents.getState().byScope);
        expect(lists).toHaveLength(1);
        expect(lists[0]).toHaveLength(1);
        expect(lists[0]?.[0]).toMatchObject({ group, href });
      },
      { timeout: 10_000 },
    );
    const item = Object.values(useRecents.getState().byScope)[0]?.[0];
    expect(item?.title).toContain(title);
    expect(item?.secondary).toBeTruthy();
  });

  it("a list page records nothing", async () => {
    const { findByRole } = renderApp("/datasets");
    await findByRole("table", { name: "Datasets" }, { timeout: 10_000 });
    expect(useRecents.getState().byScope).toEqual({});
  });
});
