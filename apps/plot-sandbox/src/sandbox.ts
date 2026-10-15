/**
 * The sandbox side of the protocol: validates inbound messages, renders, reports size,
 * selections, exports and errors back to the embedding app.
 */
import {
  errorMessage,
  findUnsafeConstructs,
  parseParentMessage,
  PROTOCOL,
  type PlotKind,
  type RenderMessage,
  type SandboxMessage,
  type Theme,
} from "./protocol";
import type { Rendered, Renderer } from "./renderers";

export interface ParentLike {
  postMessage(message: unknown, targetOrigin: string): void;
}

export interface SandboxOptions {
  parent: ParentLike;
  appOrigins: readonly string[];
  container: HTMLElement;
  loadRenderer: (kind: PlotKind) => Promise<Renderer>;
  setTheme?: (theme: Theme) => void;
}

export class SandboxController {
  private parentOrigin: string | null = null;
  private current: { id: string; rendered: Rendered } | null = null;
  private lastRender: RenderMessage | null = null;
  private token = 0;
  private lastHeight = -1;

  constructor(private readonly opts: SandboxOptions) {}

  /** Announce readiness to every configured app origin (only the real parent receives it). */
  announce(): void {
    this.post({ protocol: PROTOCOL, type: "ready" });
  }

  /** `message` event handler. Returns true if the message was accepted. */
  async handle(event: Pick<MessageEvent, "data" | "origin" | "source">): Promise<boolean> {
    if (event.source !== (this.opts.parent as unknown) || !this.opts.appOrigins.includes(event.origin)) {
      return false; // not from our embedder: ignore silently
    }
    this.parentOrigin = event.origin;
    const msg = parseParentMessage(event.data);
    if (!msg) {
      this.post(errorMessage(null, "INVALID_MESSAGE", "message rejected by the plot sandbox"));
      return false;
    }
    switch (msg.type) {
      case "render":
        await this.render(msg);
        return true;
      case "theme":
        this.opts.setTheme?.(msg.theme);
        if (this.lastRender && this.lastRender.theme !== msg.theme) {
          await this.render({ ...this.lastRender, theme: msg.theme });
        }
        return true;
      case "export":
        await this.export(msg.id, msg.format);
        return true;
    }
  }

  /** Report the document height (called from a ResizeObserver). */
  reportHeight(height: number): void {
    const h = Math.max(0, Math.min(20_000, Math.ceil(height)));
    if (h === this.lastHeight) return;
    this.lastHeight = h;
    this.post({ protocol: PROTOCOL, type: "resize", height: h });
  }

  private async render(msg: RenderMessage): Promise<void> {
    const token = ++this.token;
    this.lastRender = msg;
    const unsafe = findUnsafeConstructs(msg.kind, msg.spec);
    if (unsafe.length) {
      this.teardown();
      this.post(
        errorMessage(msg.id, "UNSAFE_SPEC", `spec refused at ${unsafe.slice(0, 5).join(", ")}`),
      );
      return;
    }
    this.opts.setTheme?.(msg.theme);
    try {
      const renderer = await this.opts.loadRenderer(msg.kind);
      if (token !== this.token) return;
      this.teardown();
      const rendered = await renderer({
        container: this.opts.container,
        spec: msg.spec,
        datasets: msg.datasets,
        theme: msg.theme,
        onSelect: (items) => this.post({ protocol: PROTOCOL, type: "select", id: msg.id, items }),
      });
      if (token !== this.token) {
        rendered.destroy();
        return;
      }
      this.current = { id: msg.id, rendered };
      this.post({ protocol: PROTOCOL, type: "rendered", id: msg.id });
    } catch (err) {
      if (token !== this.token) return;
      this.post(errorMessage(msg.id, "RENDER_FAILED", describe(err)));
    }
  }

  private async export(id: string, format: "png" | "svg"): Promise<void> {
    if (!this.current || this.current.id !== id) {
      this.post(errorMessage(id, "EXPORT_FAILED", "no rendered plot with this id"));
      return;
    }
    try {
      const dataUrl = await this.current.rendered.export(format);
      this.post({ protocol: PROTOCOL, type: "exported", id, format, dataUrl });
    } catch (err) {
      this.post(errorMessage(id, "EXPORT_FAILED", describe(err)));
    }
  }

  private teardown(): void {
    if (this.current) {
      try {
        this.current.rendered.destroy();
      } catch {
        // a failing destroy must not block the next render
      }
      this.current = null;
    }
    this.opts.container.replaceChildren();
  }

  private post(msg: SandboxMessage): void {
    const targets = this.parentOrigin ? [this.parentOrigin] : this.opts.appOrigins;
    for (const origin of targets) this.opts.parent.postMessage(msg, origin);
  }
}

function describe(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err);
  // Plain text only; the parent renders it with textContent.
  return text.replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 500) || "rendering failed";
}
