import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useDebounced } from "./use-debounced";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("useDebounced", () => {
  it("gives a new text only after it stops changing for the wait", () => {
    const { result, rerender } = renderHook(({ v }) => useDebounced(v, 250), { initialProps: { v: "a" } });
    expect(result.current).toBe("a");
    rerender({ v: "ab" });
    act(() => void vi.advanceTimersByTime(200));
    expect(result.current).toBe("a");
    rerender({ v: "abc" });
    act(() => void vi.advanceTimersByTime(200));
    expect(result.current).toBe("a");
    act(() => void vi.advanceTimersByTime(50));
    expect(result.current).toBe("abc");
  });

  it("does not start the wait again for a new object with the same content (v0.21: one hook for the library builders too)", () => {
    const { result, rerender } = renderHook(({ v }) => useDebounced(v, 250), { initialProps: { v: { name: "a", tags: ["x"] } } });
    const first = result.current;
    rerender({ v: { name: "b", tags: ["x"] } });
    act(() => void vi.advanceTimersByTime(200));
    // The same content in a new object, as a builder makes on each render.
    rerender({ v: { name: "b", tags: ["x"] } });
    expect(result.current).toBe(first);
    act(() => void vi.advanceTimersByTime(50));
    expect(result.current).toEqual({ name: "b", tags: ["x"] });
    // No more renders follow: the value keeps its identity.
    const settled = result.current;
    rerender({ v: { name: "b", tags: ["x"] } });
    act(() => void vi.advanceTimersByTime(1000));
    expect(result.current).toBe(settled);
  });
});
