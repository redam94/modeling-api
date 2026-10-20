import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { RUN_DONE, RUN_RUNNING } from "@/mocks/fixtures";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => server.resetHandlers(...createHandlers()));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Run page (v0.11)", () => {
  it("keeps an unsaved note when the user opens another tab and comes back", async () => {
    const user = userEvent.setup();
    renderApp(`/runs/${RUN_DONE}?tab=notes`);
    const note = await screen.findByRole("textbox", { name: "Note" }, { timeout: 10_000 });
    await user.clear(note);
    await user.type(note, "check the adstock prior");
    expect(await screen.findByRole("img", { name: "Changes not saved" })).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: /Metrics/ }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Note" })).toBeNull());
    await user.click(screen.getByRole("tab", { name: /Notes/ }));
    expect(await screen.findByRole("textbox", { name: "Note" })).toHaveProperty("value", "check the adstock prior");
  });

  it("shows the error of a tag row that has a value but no key, and ignores a blank row", async () => {
    const user = userEvent.setup();
    renderApp(`/runs/${RUN_DONE}?tab=notes`);
    const note = await screen.findByRole("textbox", { name: "Note" }, { timeout: 10_000 });
    await user.type(note, " more");
    await user.click(screen.getByRole("button", { name: /Add a tag/ }));
    const save = screen.getByRole("button", { name: /Save/ });
    expect(save).toHaveProperty("disabled", false); // the blank row is ignored
    const rows = within(screen.getByRole("group", { name: "Tags" }));
    const values = rows.getAllByRole("textbox", { name: /value/ });
    await user.type(values.at(-1)!, "x");
    expect(await screen.findByText("Type a key.")).toBeTruthy();
    expect(save).toHaveProperty("disabled", true);
  });

  it("gives every label filter control its own id across metric cards", async () => {
    const user = userEvent.setup();
    const { container } = renderApp(`/runs/${RUN_RUNNING}?tab=metrics`);
    const selects = await screen.findAllByRole("combobox", { name: "Colour by" }, { timeout: 10_000 });
    expect(selects.length).toBeGreaterThan(1);
    for (const select of selects) await user.selectOptions(select, "");
    const boxes = await waitFor(() => {
      const found = container.querySelectorAll('[role="group"][aria-label="Values of chain"] [role="checkbox"]');
      expect(found.length).toBeGreaterThan(2);
      return [...found];
    });
    const ids = boxes.map((b) => b.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) expect(container.querySelectorAll(`label[for="${CSS.escape(id)}"]`).length).toBe(1);
  });
});
