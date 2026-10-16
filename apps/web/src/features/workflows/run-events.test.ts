import { describe, expect, it, vi } from "vitest";

import generated from "@/mocks/workflows.gen.json";

import { applyEvent, followWorkflowRun, parseWorkflowEvent, statusFromEvent, workflowStreamReducer } from "./run-events";
import type { WorkflowEvent, WorkflowRun } from "./types";

const run = generated.canonical.run as unknown as WorkflowRun;
const events = generated.canonical.events as unknown as WorkflowEvent[];
const enc = new TextEncoder();

function frame(event: string, data: unknown, id?: number): string {
  return `${id !== undefined ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function stream(text: string): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(enc.encode(text));
        c.close();
      },
    }),
    { headers: { "content-type": "text/event-stream" } },
  );
}

describe("workflow run events", () => {
  it("parses events and maps them to node states", () => {
    const e = parseWorkflowEvent(events[1]);
    expect(e?.type).toBe("workflow.node.queued");
    expect(parseWorkflowEvent({ type: "state", seq: 1, run_id: "r", ts: "t" })).toBeNull();
    expect(parseWorkflowEvent("nope")).toBeNull();
    expect(statusFromEvent("workflow.node.started")).toBe("running");
    expect(statusFromEvent("workflow.node.cached")).toBe("cached");
    expect(statusFromEvent("workflow.branch.decided")).toBeNull();
  });

  it("patches the cached run as events arrive", () => {
    const pending: WorkflowRun = { ...run, status: "queued", nodes: run.nodes!.map((n) => ({ ...n, status: "pending" as const })) };
    const started = applyEvent(pending, { seq: 9, type: "workflow.node.started", run_id: run.run_id, node_id: "adf", ts: "2026-10-14T09:00:01Z", data: {} });
    expect(started.status).toBe("running");
    expect(started.nodes!.find((n) => n.node_id === "adf")!.status).toBe("running");
    const done = applyEvent(started, { seq: 10, type: "workflow.run.cancelled", run_id: run.run_id, node_id: null, ts: "2026-10-14T09:00:02Z", data: {} });
    expect(done.status).toBe("cancelled");
  });

  it("keeps events once, in order", () => {
    let s = workflowStreamReducer({ status: "open", detail: null, events: [] }, { type: "event", event: { ...events[2]!, id: 3 } });
    s = workflowStreamReducer(s, { type: "event", event: { ...events[0]!, id: 1 } });
    s = workflowStreamReducer(s, { type: "event", event: { ...events[2]!, id: 3 } });
    expect(s.events.map((e) => e.id)).toEqual([1, 3]);
  });

  it("resumes with Last-Event-ID after a dropped stream and stops at end", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(stream(frame("snapshot", run) + frame(events[0]!.type, events[0], 1) + frame(events[1]!.type, events[1], 2)))
      .mockResolvedValueOnce(stream(frame(events[2]!.type, events[2], 3) + frame("end", { run_id: run.run_id, status: "succeeded" })));
    const seen: number[] = [];
    const onSnapshot = vi.fn();
    const onEnd = vi.fn();
    await followWorkflowRun(
      run.run_id,
      { onSnapshot, onEvent: (_e, id) => seen.push(id), onEnd },
      { signal: new AbortController().signal, fetchImpl, sleep: async () => undefined, headers: { "X-Mapi-Project": "acme/growth" } },
    );
    expect(onSnapshot).toHaveBeenCalledTimes(1);
    expect(seen).toEqual([1, 2, 3]);
    expect(onEnd).toHaveBeenCalledWith("succeeded");
    const second = fetchImpl.mock.calls[1]![1]!.headers as Record<string, string>;
    expect(second["Last-Event-ID"]).toBe("2");
    expect(second["X-Mapi-Project"]).toBe("acme/growth");
    expect(fetchImpl.mock.calls[0]![0] as string).toMatch(/\/v1\/workflow-runs\/run_[a-z0-9]+\/events$/);
  });

  it("stops for good when the run is gone", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 404 }));
    const statuses: string[] = [];
    await followWorkflowRun("run_x", { onSnapshot: vi.fn(), onEvent: vi.fn(), onEnd: vi.fn(), onStatus: (s) => statuses.push(s) }, { signal: new AbortController().signal, fetchImpl });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(statuses.at(-1)).toBe("error");
  });
});
