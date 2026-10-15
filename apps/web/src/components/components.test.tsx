import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import type { Problem } from "@/api/types";
import { PipelineCards } from "@/features/editor/pipeline-cards";
import { buildPipeline } from "@/features/editor/pipeline";
import { ChainLanes } from "@/features/runs/progress-panel";
import { LogsViewer } from "@/features/runs/logs-viewer";
import { createHandlers } from "@/mocks/handlers";
import { RUN_RUNNING, validateResponseFor } from "@/mocks/fixtures";
import { STARTER_SPEC } from "@/features/editor/starter";
import { parse } from "yaml";

import { IssueList, ProblemNotice, StateBadge } from "./common";
import { fuzzyScore } from "./shell/command-palette";

const server = setupServer(...createHandlers());
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function withQuery(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("LogsViewer", () => {
  it("renders untrusted log lines as text with ANSI removed", async () => {
    const { container } = withQuery(<LogsViewer runId={RUN_RUNNING} live={false} />);
    const log = await screen.findByRole("log", { name: "Run logs" });
    await waitFor(() => expect(log.textContent).toContain("<img src=x onerror=alert(1)> plain text only"));
    expect(container.querySelector("img")).toBeNull();
    expect(log.textContent).not.toContain("\u001b");
    expect(log.textContent).toContain("Sampling 4 chains");
  });

  it("filters lines", async () => {
    withQuery(<LogsViewer runId={RUN_RUNNING} live={false} />);
    const log = await screen.findByRole("log", { name: "Run logs" });
    await waitFor(() => expect(log.textContent).toContain("bootstrap ok"));
    await userEvent.type(screen.getByRole("textbox", { name: "Filter log lines" }), "bootstrap");
    await waitFor(() => expect(log.textContent).not.toContain("Sampling"));
  });
});

describe("ProblemNotice", () => {
  it("keeps untrusted text out of the markup", async () => {
    const problem: Problem = {
      type: "urn:mapi:error:USER_CODE_ERROR",
      title: "User or plugin code failed",
      status: 422,
      code: "USER_CODE_ERROR",
      detail: "the plugin raised",
      untrusted_text: "<script>window.pwned=1</script>\u001b[31mTraceback\u001b[0m",
      errors: [{ code: "SCHEMA_INVALID", severity: "error", pointer: "/body/kind", message: "bad kind" }],
    };
    const { container } = render(<ProblemNotice problem={problem} />);
    expect(screen.getByText("User or plugin code failed")).toBeInTheDocument();
    expect(screen.getByText("/body/kind")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Output from the run (untrusted)"));
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>window.pwned=1</script>Traceback");
  });
});

describe("StateBadge and ChainLanes", () => {
  it("marks stalled runs and shows divergences per chain", () => {
    render(
      <>
        <StateBadge state="stalled" />
        <ChainLanes chains={[{ chain: 0, draws_done: 10, draws_total: 100, tuning: true, divergences: 3 }]} />
      </>,
    );
    expect(screen.getByText("Stalled").closest("[data-state]")).toHaveAttribute("data-state", "stalled");
    expect(screen.getByRole("progressbar", { name: "Chain 0" })).toHaveAttribute("aria-valuenow", "10");
    expect(screen.getByText("3 div.")).toBeInTheDocument();
    expect(screen.getByText("tuning")).toBeInTheDocument();
  });
});

describe("PipelineCards", () => {
  it("shows step cards, the split marker, context and issues", async () => {
    const yaml = STARTER_SPEC.replace("after_step: weekly", "after_step: scale_y");
    const model = buildPipeline(parse(yaml), validateResponseFor(yaml));
    const clicked: string[] = [];
    render(<PipelineCards model={model} onIssue={(i) => clicked.push(i.code)} />);
    expect(screen.getByRole("separator", { name: "Train/holdout split" })).toBeInTheDocument();
    expect(screen.getByText(/6-step tail along time/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /LEAKAGE_RISK/ }));
    expect(clicked).toEqual(["LEAKAGE_RISK"]);
    await userEvent.click(screen.getAllByRole("button", { name: "Schema after this step" })[0]!);
    expect(screen.getAllByText("spend_local").length).toBeGreaterThan(0);
  });
});

describe("IssueList", () => {
  it("lets users jump to an issue", async () => {
    const seen: string[] = [];
    render(<IssueList issues={[{ code: "LEAKAGE_RISK", severity: "error", pointer: "/pipeline/2", message: "m" }]} onSelect={(i) => seen.push(i.pointer ?? "")} />);
    await userEvent.click(screen.getByRole("button"));
    expect(seen).toEqual(["/pipeline/2"]);
  });
});

describe("command palette scoring", () => {
  it("prefers substring matches, then subsequences", () => {
    expect(fuzzyScore("Datasets", "data")).toBeGreaterThan(fuzzyScore("Datasets", "dts"));
    expect(fuzzyScore("Datasets", "dts")).toBeGreaterThan(0);
    expect(fuzzyScore("Runs", "xyz")).toBe(0);
  });
});
