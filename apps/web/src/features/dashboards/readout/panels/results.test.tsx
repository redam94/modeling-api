import { describe, expect, it } from "vitest";

import { ShareApiError } from "../../client";
import { downloadGone, inputLabels } from "./results";
import { classifierDashboard } from "./test-fixtures";

const problem = (code: string) => ({ type: "about:blank", title: "Gone", status: 410, code });

describe("client results", () => {
  it("says the predictions expired only for DASHBOARD_DOWNLOAD_GONE, never for an ended link", () => {
    expect(downloadGone(new ShareApiError(410, problem("DASHBOARD_DOWNLOAD_GONE") as never))).toBe(true);
    expect(downloadGone(new ShareApiError(410, problem("LINK_EXPIRED") as never))).toBe(false);
    expect(downloadGone(new ShareApiError(404, null))).toBe(false);
  });

  it("names tried inputs with the analyst's labels", () => {
    const d = classifierDashboard();
    const labels = inputLabels(d);
    for (const i of d.scenario_form?.inputs ?? []) expect(labels[i.name]).toBe(i.label);
  });
});
