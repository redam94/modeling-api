/**
 * A Vega tooltip handler that never interprets markup: every key and value is written with
 * `textContent`, values are stringified and truncated, and nested objects are flattened one
 * level. Replaces vega-tooltip's HTML-building default.
 */

const MAX_ROWS = 24;
const MAX_TEXT = 200;

export function tooltipEntries(value: unknown): [string, string][] {
  if (value === null || value === undefined) return [];
  if (typeof value !== "object") return [["", clip(formatValue(value))]];
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (out.length >= MAX_ROWS) break;
    out.push([clip(k), clip(formatValue(v))]);
  }
  return out;
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toPrecision(6).replace(/\.?0+$/, "");
  if (v instanceof Date) return Number.isFinite(v.getTime()) ? v.toISOString() : "";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return "[object]";
    }
  }
  return typeof v === "string" ? v : typeof v === "boolean" || typeof v === "bigint" ? String(v) : "";
}

function clip(s: string): string {
  return s.length > MAX_TEXT ? `${s.slice(0, MAX_TEXT - 1)}…` : s;
}

export class TextTooltip {
  private readonly el: HTMLDivElement;

  constructor(private readonly doc: Document = document) {
    this.el = doc.createElement("div");
    this.el.className = "mapi-tooltip";
    this.el.setAttribute("role", "tooltip");
    this.el.hidden = true;
    doc.body.appendChild(this.el);
  }

  /** Vega `TooltipHandler` signature: (handler, event, item, value). */
  readonly handler = (_handler: unknown, event: MouseEvent, _item: unknown, value: unknown): void => {
    const entries = tooltipEntries(value);
    if (!entries.length) {
      this.el.hidden = true;
      return;
    }
    this.el.replaceChildren();
    for (const [k, v] of entries) {
      const row = this.doc.createElement("div");
      row.className = "mapi-tooltip-row";
      if (k) {
        const key = this.doc.createElement("span");
        key.className = "mapi-tooltip-key";
        key.textContent = k;
        row.appendChild(key);
      }
      const val = this.doc.createElement("span");
      val.className = "mapi-tooltip-value";
      val.textContent = v;
      row.appendChild(val);
      this.el.appendChild(row);
    }
    this.el.hidden = false;
    const pad = 12;
    const { innerWidth, innerHeight } = this.doc.defaultView ?? { innerWidth: 800, innerHeight: 600 };
    const rect = this.el.getBoundingClientRect();
    const x = Math.min(event.clientX + pad, innerWidth - rect.width - 4);
    const y = Math.min(event.clientY + pad, innerHeight - rect.height - 4);
    this.el.style.left = `${Math.max(4, x)}px`;
    this.el.style.top = `${Math.max(4, y)}px`;
  };

  hide(): void {
    this.el.hidden = true;
  }
}
