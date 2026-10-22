import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ClientDashboard, DashboardClientV2 } from "../types";
import { ScoreFile } from "./score-file";

function refused(code: string, status = 409): Error {
  return Object.assign(new Error(code), { status, problem: { code, title: "Not available", status, detail: "scoring a file (the id column 'id' is not a dim) is not available" } });
}

function view(err: Error) {
  const client = { cacheKey: ["t", Math.random()], batchTemplate: () => Promise.reject(err) } as unknown as DashboardClientV2;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ScoreFile client={client} dashboard={{} as ClientDashboard} />
    </QueryClientProvider>,
  );
}

describe("ScoreFile", () => {
  it("says plainly that scoring a file is not available when the section is disabled (R4)", async () => {
    view(refused("DASHBOARD_INPUT_DISABLED"));
    expect(await screen.findByText(/^Scoring a file is not available on this dashboard yet\./)).toBeInTheDocument();
    expect(screen.queryByText(/id column/)).toBeNull();
  });

  it("keeps other errors as errors", async () => {
    view(refused("INTERNAL_ERROR", 500));
    expect(await screen.findByText(/Not available\. scoring a file/)).toBeInTheDocument();
  });
});
