import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryParam } from "@/lib/list-query";

import { countLine, ListNoMatches, ListToolbar, matchesQuery } from "./list-toolbar";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function type(box: HTMLElement, text: string) {
  fireEvent.change(box, { target: { value: text } });
}

describe("ListToolbar (v0.21: one toolbar for the list pages)", () => {
  it("is a search box named after the things, with the query of the URL in it", () => {
    render(<ListToolbar things="datasets" value="media" onChange={() => undefined} />);
    const box = screen.getByRole("searchbox", { name: "Search datasets" });
    expect(box).toHaveAttribute("type", "search");
    expect(box).toHaveValue("media");
    expect(box).toHaveAttribute("placeholder", "Search datasets");
  });

  it("gives the typed text once, 250 ms after the last key, without the spaces around it", () => {
    const onChange = vi.fn();
    render(<ListToolbar things="datasets" value="" onChange={onChange} />);
    const box = screen.getByRole("searchbox", { name: "Search datasets" });
    type(box, "eu");
    act(() => void vi.advanceTimersByTime(200));
    type(box, " eu media ");
    act(() => void vi.advanceTimersByTime(249));
    expect(onChange).not.toHaveBeenCalled();
    act(() => void vi.advanceTimersByTime(1));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("eu media");
    // The box keeps what the user typed.
    expect(box).toHaveValue(" eu media ");
  });

  it("has a clear button only when the box has text; it empties the box at once", () => {
    const onChange = vi.fn();
    function Page() {
      const [q, setQ] = useState("media");
      return (
        <ListToolbar
          things="datasets"
          value={q}
          onChange={(next) => {
            onChange(next);
            setQ(next);
          }}
        />
      );
    }
    render(<Page />);
    const box = screen.getByRole("searchbox", { name: "Search datasets" });
    fireEvent.click(screen.getByRole("button", { name: "Clear the search box" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("");
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Clear the search box" })).toBeNull();
    // The wait that follows does not send the empty text a second time.
    act(() => void vi.advanceTimersByTime(1000));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("takes a new query from outside (a link, Back), and keeps the text while its own query comes back", () => {
    const onChange = vi.fn();
    const { rerender } = render(<ListToolbar things="runs" value="" onChange={onChange} />);
    const box = screen.getByRole("searchbox", { name: "Search runs" });
    rerender(<ListToolbar things="runs" value="grid" onChange={onChange} />);
    expect(box).toHaveValue("grid");
    // The user types on; the URL then gets the text that was sent before.
    type(box, "grid s");
    act(() => void vi.advanceTimersByTime(250));
    expect(onChange).toHaveBeenLastCalledWith("grid s");
    type(box, "grid se");
    rerender(<ListToolbar things="runs" value="grid s" onChange={onChange} />);
    expect(box).toHaveValue("grid se");
    act(() => void vi.advanceTimersByTime(250));
    expect(onChange).toHaveBeenLastCalledWith("grid se");
  });

  it("shows the count line only when a query is active", () => {
    const { rerender } = render(<ListToolbar things="datasets" value="" onChange={() => undefined} count={{ shown: 41, total: 41 }} />);
    // The live region is there before it has text, so a screen reader says the first count.
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    rerender(<ListToolbar things="datasets" value="media" onChange={() => undefined} count={{ shown: 3, total: 41 }} />);
    const line = screen.getByTestId("list-count");
    expect(line).toHaveTextContent("3 of 41 datasets");
    expect(line).toHaveAttribute("role", "status");
  });

  it("puts filters in the slot after the search box", () => {
    render(
      <ListToolbar things="experiments" value="" onChange={() => undefined}>
        <button type="button">Show archived</button>
      </ListToolbar>,
    );
    const bar = screen.getByRole("search", { name: "Search and filter experiments" });
    expect(bar).toContainElement(screen.getByRole("searchbox", { name: "Search experiments" }));
    expect(bar).toContainElement(screen.getByRole("button", { name: "Show archived" }));
  });

  it("takes another name and placeholder for the box", () => {
    render(<ListToolbar things="catalog items" label="Search the catalog" placeholder="Type a word" value="" onChange={() => undefined} />);
    expect(screen.getByRole("searchbox", { name: "Search the catalog" })).toHaveAttribute("placeholder", "Type a word");
  });
});

describe("countLine", () => {
  it("says how many of the loaded things match", () => {
    expect(countLine("datasets", { shown: 3, total: 41 })).toBe("3 of 41 datasets");
    expect(countLine("datasets", { shown: 0, total: 41 })).toBe("0 of 41 datasets");
  });

  it("says that it counts the first page only when the list has more pages", () => {
    expect(countLine("datasets", { shown: 3, total: 200, more: true })).toBe("3 in the first 200 datasets");
  });

  it("counts what the server found when the server filters", () => {
    expect(countLine("runs", { shown: 3 })).toBe("3 runs");
    expect(countLine("runs", { shown: 1 })).toBe("1 run");
    expect(countLine("catalog items", { shown: 1 })).toBe("1 catalog item");
    expect(countLine("runs", { shown: 50, more: true })).toBe("The first 50 runs");
  });
});

describe("matchesQuery", () => {
  it("accepts everything for an empty query", () => {
    expect(matchesQuery("", ["a"])).toBe(true);
    expect(matchesQuery("   ", [null])).toBe(true);
  });

  it("needs each word of the query in a name or in the other text, in any case", () => {
    expect(matchesQuery("EU media", ["eu_media_daily"])).toBe(true);
    expect(matchesQuery("media weekly", ["eu_media_daily"], ["Weekly spend by market"])).toBe(true);
    expect(matchesQuery("media monthly", ["eu_media_daily"], ["Weekly spend by market"])).toBe(false);
    expect(matchesQuery("zzz", ["eu_media_daily", null, undefined])).toBe(false);
  });

  it("finds a name by its letters in order, as the global search does, but not the other text", () => {
    expect(matchesQuery("dts", ["Datasets"])).toBe(true);
    expect(matchesQuery("dts", ["Models"], ["Datasets of the project"])).toBe(false);
  });
});

describe("queryParam (the ?q= of a list page)", () => {
  it("keeps a text, without the spaces around it", () => {
    expect(queryParam(" eu media ")).toBe("eu media");
  });

  it("gives the text of a number or a boolean (the router reads ?q=2025 as a number)", () => {
    expect(queryParam(2025)).toBe("2025");
    expect(queryParam(true)).toBe("true");
  });

  it("drops an empty text, a long text and other values", () => {
    expect(queryParam("")).toBeUndefined();
    expect(queryParam("   ")).toBeUndefined();
    expect(queryParam("x".repeat(201))).toBeUndefined();
    expect(queryParam(["a"])).toBeUndefined();
    expect(queryParam({ a: 1 })).toBeUndefined();
    expect(queryParam(undefined)).toBeUndefined();
  });
});

describe("ListNoMatches", () => {
  it("shows the search glyph, one sentence and a button that clears the search", () => {
    const onClear = vi.fn();
    const { container } = render(<ListNoMatches things="datasets" query="zzz" onClear={onClear} />);
    expect(container.querySelector("[data-slot='brand-art']")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "No datasets match" })).toBeInTheDocument();
    expect(screen.getByText("The app found no datasets for “zzz”.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear the search" }));
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("names the other filters when they are on", () => {
    render(<ListNoMatches things="dashboards" query="zzz" filtered onClear={() => undefined} />);
    expect(screen.getByText("The app found no dashboards for “zzz” with these filters.")).toBeInTheDocument();
  });

  it("says that it looked at the first page only when the list has more pages", () => {
    render(<ListNoMatches things="datasets" query="zzz" firstPage={200} onClear={() => undefined} />);
    expect(screen.getByText("The app found no match for “zzz” in the first 200 datasets.")).toBeInTheDocument();
  });
});
