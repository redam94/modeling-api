import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { Problem } from "@/api/types";
import { env } from "@/env";
import { PipelineCards } from "@/features/editor/pipeline-cards";
import { buildPipeline } from "@/features/editor/pipeline";
import { ChainLanes } from "@/features/runs/progress-panel";
import { LogsViewer } from "@/features/runs/logs-viewer";
import { createHandlers } from "@/mocks/handlers";
import { artifactLink, profileView, RUN_RUNNING, validateResponseFor } from "@/mocks/fixtures";
import { ScoreResults } from "@/features/score/results";
import { STARTER_SPEC } from "@/features/editor/starter";
import { parse } from "yaml";

import { IssueList, ProblemNotice, StateBadge } from "./common";
import { Field, Input } from "./ui/primitives";
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
  it("follows a long live log one page per poll, not in a burst that trips the rate limit", async () => {
    let requests = 0;
    server.use(
      http.get(`${env.apiBase}/v1/runs/:id/logs`, ({ request }) => {
        requests += 1;
        const start = Number(new URL(request.url).searchParams.get("cursor") ?? 0);
        const items = Array.from({ length: 200 }, (_, i) => ({
          run_id: RUN_RUNNING, attempt: 1, seq: start + i + 1, ts: "2026-10-15T09:30:00Z", stream: "stdout" as const, line: `line ${start + i + 1}`,
        }));
        return HttpResponse.json({ items, next_cursor: String(start + 200) }); // always more: far behind
      }),
    );
    withQuery(<LogsViewer runId={RUN_RUNNING} live />);
    await screen.findByText("Loading newer lines…");
    await new Promise((r) => setTimeout(r, 1200));
    expect(requests).toBe(1);
  });

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
    const yaml = STARTER_SPEC.replace("after_step: weekly", "after_step: fill");
    const model = buildPipeline(parse(yaml), validateResponseFor(yaml));
    const clicked: string[] = [];
    // M3.5: steps and the model show their About cards (GET /v1/catalog/abouts).
    withQuery(<PipelineCards model={model} onIssue={(i) => clicked.push(i.code)} />);
    expect(screen.getByRole("separator", { name: "Train/holdout split" })).toBeInTheDocument();
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

describe("Field", () => {
  it("shows an error apart from the help text and ties both to the control", () => {
    render(
      <Field label="Draws" htmlFor="draws" hint="0 returns summaries only." error="Draws must be between 0 and 10000.">
        <Input id="draws" />
      </Field>,
    );
    const input = screen.getByLabelText("Draws");
    expect(input).toHaveAttribute("aria-invalid", "true");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Draws must be between 0 and 10000.");
    expect(alert).toHaveClass("text-destructive");
    // The help text stays, and both are announced with the field.
    expect(screen.getByText("0 returns summaries only.")).toBeInTheDocument();
    expect(input).toHaveAccessibleDescription("Draws must be between 0 and 10000. 0 returns summaries only.");
  });

  it("leaves a valid control unmarked", () => {
    render(
      <Field label="Seed" htmlFor="seed" hint="Optional.">
        <Input id="seed" />
      </Field>,
    );
    const input = screen.getByLabelText("Seed");
    expect(input).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(input).toHaveAccessibleDescription("Optional.");
  });
});

describe("ScoreResults download", () => {
  it("saves the verified bytes as a file instead of navigating to the storage origin", async () => {
    const bytes = new TextEncoder().encode(JSON.stringify(profileView));
    const link = artifactLink("profile.json", bytes, "application/json");
    const blobs: Blob[] = [];
    const created = vi.fn((b: Blob) => {
      blobs.push(b);
      return "blob:http://127.0.0.1:5173/saved";
    });
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() });
    const clicks: { href: string; download: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push({ href: this.href, download: this.download });
    });
    withQuery(<ScoreResults outputs={[link]} />);
    // No link to the signed URL: a cross-origin <a download> would replace the app.
    expect(document.querySelector(`a[href^="http://127.0.0.1:4443"]`)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Download profile.json" }));
    await waitFor(() => expect(clicks).toHaveLength(1));
    expect(clicks[0]).toEqual({ href: "blob:http://127.0.0.1:5173/saved", download: "profile.json" });
    expect(blobs[0]?.size).toBe(bytes.length);
  });
});

describe("command palette scoring", () => {
  it("prefers substring matches, then subsequences", () => {
    expect(fuzzyScore("Datasets", "data")).toBeGreaterThan(fuzzyScore("Datasets", "dts"));
    expect(fuzzyScore("Datasets", "dts")).toBeGreaterThan(0);
    expect(fuzzyScore("Runs", "xyz")).toBe(0);
  });
});
