import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { RUN_FIT_A, RUN_FIT_B } from "@/mocks/fixtures";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const PAGE = `/runs/compare?runs=${encodeURIComponent(JSON.stringify([RUN_FIT_A, RUN_FIT_B]))}`;

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
  // jsdom has no object URLs (the media blocks show images through them)
  URL.createObjectURL ??= () => "blob:test";
  URL.revokeObjectURL ??= () => undefined;
});
beforeEach(() => server.resetHandlers(...createHandlers()));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Compare page (v0.14)", () => {
  it("clears the last media key instead of falling back to the default one", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(PAGE);
    await user.click(await screen.findByRole("tab", { name: "Media" }, { timeout: 10_000 }));
    const keys = within(await screen.findByRole("group", { name: "Media keys" }));
    const samples = await keys.findByRole("button", { name: /samples/ });
    await waitFor(() => expect(samples.getAttribute("aria-pressed")).toBe("true"));
    await user.click(samples);
    await waitFor(() => expect(router.state.location.search).toMatchObject({ media: [] }));
    expect(keys.getByRole("button", { name: /samples/ }).getAttribute("aria-pressed")).toBe("false");
  });

  it("clears the last curve key instead of drawing every default key again", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(PAGE);
    const picker = await screen.findByRole("group", { name: "Metrics to draw" }, { timeout: 10_000 });
    const boxes = () => within(picker).getAllByRole("checkbox");
    await waitFor(() => expect(boxes().some((b) => b.getAttribute("aria-checked") === "true")).toBe(true));
    for (const box of boxes()) if (box.getAttribute("aria-checked") === "true") await user.click(box);
    await waitFor(() => expect(router.state.location.search).toMatchObject({ keys: [] }));
    expect(boxes().every((b) => b.getAttribute("aria-checked") === "false")).toBe(true);
    expect(await screen.findByText("Select the metrics to draw.")).toBeTruthy();
  });
});
