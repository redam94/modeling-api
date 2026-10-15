import { describe, expect, it, vi } from "vitest";

import { exportMessage, PROTOCOL, renderMessage, themeMessage } from "./protocol";
import type { Renderer } from "./renderers";
import { SandboxController } from "./sandbox";

const APP = "http://127.0.0.1:5173";

function setup(renderer?: Renderer) {
  const parent = { postMessage: vi.fn() };
  const container = document.createElement("div");
  const destroy = vi.fn();
  const fake: Renderer =
    renderer ??
    (async (ctx) => {
      ctx.container.textContent = "plot";
      return { export: async (f) => (f === "png" ? "data:image/png;base64,AAAA" : "data:image/svg+xml;base64,PHN2Zz4="), destroy };
    });
  const loadRenderer = vi.fn(async () => fake);
  const setTheme = vi.fn();
  const controller = new SandboxController({ parent, appOrigins: [APP], container, loadRenderer, setTheme });
  const send = (data: unknown, origin = APP, source: unknown = parent) =>
    controller.handle({ data, origin, source: source as MessageEventSource });
  return { parent, container, controller, send, loadRenderer, destroy, setTheme };
}

const posted = (parent: { postMessage: ReturnType<typeof vi.fn> }) =>
  parent.postMessage.mock.calls.map((c) => c[0] as { type: string });

describe("SandboxController", () => {
  it("announces readiness to the configured origins", () => {
    const { controller, parent } = setup();
    controller.announce();
    expect(parent.postMessage).toHaveBeenCalledWith({ protocol: PROTOCOL, type: "ready" }, APP);
  });

  it("ignores messages from other windows or origins", async () => {
    const { send, parent, loadRenderer } = setup();
    const msg = renderMessage("p", "vega-lite", { mark: "bar" }, {}, "light");
    expect(await send(msg, "https://evil.test")).toBe(false);
    expect(await send(msg, APP, {})).toBe(false);
    expect(loadRenderer).not.toHaveBeenCalled();
    expect(parent.postMessage).not.toHaveBeenCalled();
  });

  it("reports invalid messages from the parent without acting on them", async () => {
    const { send, parent, loadRenderer } = setup();
    expect(await send({ protocol: PROTOCOL, type: "navigate", url: "x" })).toBe(false);
    expect(loadRenderer).not.toHaveBeenCalled();
    expect(posted(parent)).toEqual([expect.objectContaining({ type: "error", code: "INVALID_MESSAGE" })]);
  });

  it("renders, answers to the parent origin and exports", async () => {
    const { send, parent, container, setTheme } = setup();
    await send(renderMessage("p1", "vega-lite", { mark: "bar" }, {}, "dark"));
    expect(container.textContent).toBe("plot");
    expect(setTheme).toHaveBeenCalledWith("dark");
    expect(parent.postMessage).toHaveBeenLastCalledWith({ protocol: PROTOCOL, type: "rendered", id: "p1" }, APP);
    await send(exportMessage("p1", "png"));
    expect(posted(parent).at(-1)).toEqual(
      expect.objectContaining({ type: "exported", id: "p1", dataUrl: "data:image/png;base64,AAAA" }),
    );
  });

  it("refuses unsafe specs before loading a renderer", async () => {
    const { send, parent, loadRenderer } = setup();
    await send(renderMessage("p1", "vega-lite", { data: { url: "https://evil.test" }, mark: "bar" }, {}, "dark"));
    expect(loadRenderer).not.toHaveBeenCalled();
    expect(posted(parent).at(-1)).toEqual(expect.objectContaining({ type: "error", code: "UNSAFE_SPEC" }));
  });

  it("reports renderer failures as RENDER_FAILED with plain text", async () => {
    const { send, parent } = setup(async () => {
      throw new Error("bad\u001b[31m spec");
    });
    await send(renderMessage("p1", "plotly", { data: [] }, {}, "dark"));
    expect(posted(parent).at(-1)).toEqual(
      expect.objectContaining({ type: "error", code: "RENDER_FAILED", message: "bad [31m spec" }),
    );
  });

  it("destroys the previous plot when re-rendering and re-renders on theme change", async () => {
    const { send, destroy, loadRenderer } = setup();
    await send(renderMessage("p1", "vega-lite", { mark: "bar" }, {}, "dark"));
    await send(themeMessage("light"));
    expect(destroy).toHaveBeenCalledTimes(1);
    expect(loadRenderer).toHaveBeenCalledTimes(2);
  });

  it("export of an unknown plot fails cleanly", async () => {
    const { send, parent } = setup();
    await send(exportMessage("nope", "svg"));
    expect(posted(parent).at(-1)).toEqual(expect.objectContaining({ type: "error", code: "EXPORT_FAILED" }));
  });

  it("reports height changes once", () => {
    const { controller, parent } = setup();
    controller.reportHeight(200.2);
    controller.reportHeight(201);
    expect(parent.postMessage).toHaveBeenCalledTimes(1);
    expect(parent.postMessage).toHaveBeenCalledWith({ protocol: PROTOCOL, type: "resize", height: 201 }, APP);
  });
});
