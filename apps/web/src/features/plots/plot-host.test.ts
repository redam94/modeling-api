import { PROTOCOL } from "@mapi/plot-sandbox/protocol";
import { describe, expect, it, vi } from "vitest";

import { MAX_HEIGHT, MIN_HEIGHT, OPAQUE_ORIGIN, PlotHost, type FrameLike, type WindowLike } from "./plot-host";

function setup() {
  const contentWindow = { postMessage: vi.fn() };
  let onLoad: (() => void) | null = null;
  const frame: FrameLike = {
    contentWindow,
    addEventListener: (_t, l) => (onLoad = l),
    removeEventListener: () => (onLoad = null),
  };
  let onMessage: ((e: MessageEvent) => void) | null = null;
  const win: WindowLike = {
    addEventListener: (_t, l) => (onMessage = l),
    removeEventListener: () => (onMessage = null),
  };
  const cb = {
    onReady: vi.fn(),
    onRendered: vi.fn(),
    onResize: vi.fn(),
    onSelect: vi.fn(),
    onExported: vi.fn(),
    onError: vi.fn(),
  };
  const host = new PlotHost(frame, cb, win);
  host.attach();
  const deliver = (data: unknown, opts: { origin?: string; source?: unknown } = {}) =>
    onMessage?.({ data, origin: opts.origin ?? OPAQUE_ORIGIN, source: (opts.source ?? contentWindow) as MessageEventSource } as MessageEvent);
  return { host, cb, contentWindow, deliver, load: () => onLoad?.() };
}

const ready = { protocol: PROTOCOL, type: "ready" };

describe("PlotHost", () => {
  it("queues the latest render until the sandbox is ready, then posts it with target '*'", () => {
    const { host, contentWindow, deliver, cb } = setup();
    host.render("vega-lite", { mark: "bar" }, {}, "light");
    const id = host.render("vega-lite", { mark: "line" }, {}, "dark");
    expect(contentWindow.postMessage).not.toHaveBeenCalled();
    deliver(ready);
    expect(cb.onReady).toHaveBeenCalled();
    expect(contentWindow.postMessage).toHaveBeenCalledTimes(1);
    const [msg, target] = contentWindow.postMessage.mock.calls[0] as [Record<string, unknown>, string];
    expect(target).toBe("*");
    expect(msg).toMatchObject({ type: "render", id, theme: "dark", spec: { mark: "line" } });
  });

  it("ignores messages from other windows or non-opaque origins", () => {
    const { host, deliver, cb, contentWindow } = setup();
    host.render("vega-lite", { mark: "bar" }, {}, "light");
    deliver(ready, { source: {} });
    deliver(ready, { origin: "http://127.0.0.1:5174" });
    deliver(ready, { origin: "https://evil.test" });
    expect(cb.onReady).not.toHaveBeenCalled();
    expect(contentWindow.postMessage).not.toHaveBeenCalled();
  });

  it("drops invalid messages silently", () => {
    const { deliver, cb } = setup();
    deliver({ protocol: PROTOCOL, type: "navigate", url: "https://evil.test" });
    deliver({ protocol: PROTOCOL, type: "resize", height: "999" });
    deliver("ready");
    expect(cb.onReady).not.toHaveBeenCalled();
    expect(cb.onResize).not.toHaveBeenCalled();
    expect(cb.onError).not.toHaveBeenCalled();
  });

  it("clamps resize heights and routes rendered/select/export/error for the current plot only", () => {
    const { host, deliver, cb } = setup();
    deliver(ready);
    const id = host.render("plotly", { data: [] }, {}, "light");
    deliver({ protocol: PROTOCOL, type: "resize", height: 10 });
    deliver({ protocol: PROTOCOL, type: "resize", height: 20_000 });
    expect(cb.onResize).toHaveBeenNthCalledWith(1, MIN_HEIGHT);
    expect(cb.onResize).toHaveBeenNthCalledWith(2, MAX_HEIGHT);
    deliver({ protocol: PROTOCOL, type: "rendered", id: "plot-stale" });
    expect(cb.onRendered).not.toHaveBeenCalled();
    deliver({ protocol: PROTOCOL, type: "rendered", id });
    expect(cb.onRendered).toHaveBeenCalledWith(id);
    deliver({ protocol: PROTOCOL, type: "select", id, items: [{ x: 1 }] });
    expect(cb.onSelect).toHaveBeenCalledWith([{ x: 1 }]);
    host.requestExport("png");
    deliver({ protocol: PROTOCOL, type: "exported", id, format: "png", dataUrl: "data:image/png;base64,AAAA" });
    expect(cb.onExported).toHaveBeenCalledWith("png", "data:image/png;base64,AAAA");
    deliver({ protocol: PROTOCOL, type: "error", id: null, code: "RENDER_FAILED", message: "x" });
    expect(cb.onError).toHaveBeenCalledWith("RENDER_FAILED", "x");
  });

  it("accepts exactly one 'exported' reply per requestExport, matching id and format", () => {
    const { host, deliver, cb } = setup();
    deliver(ready);
    const id = host.render("vega-lite", { mark: "bar" }, {}, "light");
    const svg = { protocol: PROTOCOL, type: "exported", id, format: "svg", dataUrl: "data:image/svg+xml;base64,PHN2Zz4=" };
    const png = { protocol: PROTOCOL, type: "exported", id, format: "png", dataUrl: "data:image/png;base64,AAAA" };
    // Unsolicited: nothing was requested, so a (possibly compromised) sandbox cannot force downloads.
    deliver(svg);
    deliver(png);
    expect(cb.onExported).not.toHaveBeenCalled();
    host.requestExport("png");
    deliver(svg); // wrong format for the pending request
    expect(cb.onExported).not.toHaveBeenCalled();
    deliver(png);
    deliver(png); // a replay after the single reply is dropped
    expect(cb.onExported).toHaveBeenCalledTimes(1);
    expect(cb.onExported).toHaveBeenCalledWith("png", png.dataUrl);
  });

  it("drops a pending export when a new plot is rendered", () => {
    const { host, deliver, cb } = setup();
    deliver(ready);
    const first = host.render("vega-lite", { mark: "bar" }, {}, "light");
    host.requestExport("svg");
    const second = host.render("vega-lite", { mark: "line" }, {}, "light");
    deliver({ protocol: PROTOCOL, type: "exported", id: first, format: "svg", dataUrl: "data:image/svg+xml;base64,PHN2Zz4=" });
    deliver({ protocol: PROTOCOL, type: "exported", id: second, format: "svg", dataUrl: "data:image/svg+xml;base64,PHN2Zz4=" });
    expect(cb.onExported).not.toHaveBeenCalled();
  });

  it("stops talking to the frame if it navigates after the first load", () => {
    const { host, deliver, cb, contentWindow, load } = setup();
    load();
    deliver(ready);
    host.render("vega-lite", { mark: "bar" }, {}, "light");
    expect(contentWindow.postMessage).toHaveBeenCalledTimes(1);
    load(); // second load = the sandbox navigated
    expect(host.isCompromised).toBe(true);
    expect(cb.onError).toHaveBeenCalledWith("COMPROMISED", expect.any(String));
    host.render("vega-lite", { mark: "bar" }, { secret: [{ v: 1 }] }, "light");
    host.requestExport("png");
    deliver(ready);
    expect(contentWindow.postMessage).toHaveBeenCalledTimes(1);
  });

  it("detaches its listeners", () => {
    const { host, deliver, cb } = setup();
    host.detach();
    deliver(ready);
    expect(cb.onReady).not.toHaveBeenCalled();
  });
});
