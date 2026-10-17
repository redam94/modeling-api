/**
 * Parent side of the plot sandbox protocol (plan §10.3).
 *
 * The iframe is `sandbox="allow-scripts"` without `allow-same-origin`, so its document has an
 * opaque origin and its messages arrive with `event.origin === "null"`. Trust therefore rests
 * on `event.source === iframe.contentWindow` plus the opaque origin, and every inbound message
 * is validated with `parseSandboxMessage`. Messages never cause navigation or API calls.
 * Outbound messages use target origin "*" (an opaque origin cannot be named); if the frame
 * ever navigates after its first load, the host stops talking to it and reports compromise.
 * An `exported` reply is accepted only once, for the export `requestExport` actually asked for.
 */
import {
  exportMessage,
  parseSandboxMessage,
  renderMessage,
  themeMessage,
  type JsonObject,
  type ParentMessage,
  type PlotDatasets,
  type PlotKind,
  type Row,
  type SandboxErrorCode,
  type Theme,
} from "@mapi/plot-sandbox/protocol";

export const OPAQUE_ORIGIN = "null";
export const MIN_HEIGHT = 120;
export const MAX_HEIGHT = 2400;

export interface PlotHostCallbacks {
  onReady?(): void;
  onRendered?(id: string): void;
  onResize?(height: number): void;
  onSelect?(items: Row[]): void;
  onExported?(format: "png" | "svg", dataUrl: string): void;
  onError?(code: SandboxErrorCode | "COMPROMISED", message: string): void;
}

export interface FrameLike {
  readonly contentWindow: { postMessage(message: unknown, targetOrigin: string): void } | null;
  addEventListener(type: "load", listener: () => void): void;
  removeEventListener(type: "load", listener: () => void): void;
}

export interface WindowLike {
  addEventListener(type: "message", listener: (e: MessageEvent) => void): void;
  removeEventListener(type: "message", listener: (e: MessageEvent) => void): void;
}

let counter = 0;

export class PlotHost {
  private ready = false;
  private loads = 0;
  private compromised = false;
  private pending: ParentMessage | null = null;
  private currentId: string | null = null;
  /** The one export the parent asked for; an `exported` reply is honoured only if it matches. */
  private pendingExport: { id: string; format: "png" | "svg" } | null = null;

  constructor(
    private readonly frame: FrameLike,
    private readonly callbacks: PlotHostCallbacks,
    private readonly win: WindowLike = window,
  ) {}

  attach(): void {
    this.win.addEventListener("message", this.onMessage);
    this.frame.addEventListener("load", this.onLoad);
  }

  detach(): void {
    this.win.removeEventListener("message", this.onMessage);
    this.frame.removeEventListener("load", this.onLoad);
    this.ready = false;
  }

  get isCompromised(): boolean {
    return this.compromised;
  }

  render(kind: PlotKind, spec: JsonObject, datasets: PlotDatasets, theme: Theme): string {
    counter += 1;
    const id = `plot-${counter}`;
    this.currentId = id;
    this.pendingExport = null;
    this.send(renderMessage(id, kind, spec, datasets, theme));
    return id;
  }

  setTheme(theme: Theme): void {
    if (this.currentId) this.send(themeMessage(theme));
  }

  requestExport(format: "png" | "svg"): void {
    if (!this.currentId) return;
    if (this.send(exportMessage(this.currentId, format))) {
      this.pendingExport = { id: this.currentId, format };
    }
  }

  /** Posts to the sandbox; returns whether the message was actually sent. */
  private send(msg: ParentMessage): boolean {
    if (this.compromised) return false;
    if (!this.ready) {
      // Only the latest render matters before the sandbox is ready.
      if (msg.type === "render") this.pending = msg;
      return false;
    }
    const target = this.frame.contentWindow;
    if (!target) return false;
    target.postMessage(msg, "*");
    return true;
  }

  private readonly onLoad = (): void => {
    this.loads += 1;
    if (this.loads > 1) {
      // The sandbox navigated itself (or was navigated): never post data to it again.
      this.compromised = true;
      this.ready = false;
      this.pending = null;
      this.callbacks.onError?.("COMPROMISED", "The plot frame went to a different page. The app stopped the plot.");
    }
  };

  private readonly onMessage = (event: MessageEvent): void => {
    if (this.compromised) return;
    const source = this.frame.contentWindow;
    if (!source || event.source !== (source as unknown as MessageEventSource)) return;
    if (event.origin !== OPAQUE_ORIGIN) return;
    const msg = parseSandboxMessage(event.data);
    if (!msg) return; // invalid messages are dropped silently
    switch (msg.type) {
      case "ready": {
        this.ready = true;
        this.callbacks.onReady?.();
        const pending = this.pending;
        this.pending = null;
        if (pending) this.send(pending);
        break;
      }
      case "rendered":
        if (msg.id === this.currentId) this.callbacks.onRendered?.(msg.id);
        break;
      case "resize":
        this.callbacks.onResize?.(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, msg.height)));
        break;
      case "select":
        if (msg.id === this.currentId) this.callbacks.onSelect?.(msg.items);
        break;
      case "exported": {
        // Only a reply to an export the parent asked for, exactly once: a compromised sandbox
        // must not be able to push unsolicited (or repeated) file downloads through the app.
        const want = this.pendingExport;
        if (!want || msg.id !== this.currentId || msg.id !== want.id || msg.format !== want.format) break;
        this.pendingExport = null;
        this.callbacks.onExported?.(msg.format, msg.dataUrl);
        break;
      }
      case "error":
        if (msg.id === null || msg.id === this.currentId) this.callbacks.onError?.(msg.code, msg.message);
        break;
    }
  };
}
