/**
 * The field viewer in the Model Explorer against the mock API (`demo-fields`, served from
 * `fields.gen.json`): the statistic switch, the frame slider, the draw scrubber and the
 * keyboard readout. jsdom has no canvas, so the pixels themselves are tested in fields.test.ts.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { EXTRA_MODEL_IDS } from "@/mocks/explore";
import { createHandlers } from "@/mocks/handlers";
import { renderApp } from "@/test/render-app";
import { useUi } from "@/stores/ui";

import { FieldViewer } from "./field-viewer";
import { encodeRaster } from "./fields";
import { scienceExample } from "./science-examples.test-helper";

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
});
afterEach(() => {
  server.resetHandlers();
  useUi.setState({ persona: null, personas: {} });
});
afterAll(() => server.close());

const T = { timeout: 5000 };

describe("field viewer", () => {
  it("switches statistics, frames and draws and reads a cell with the keyboard", async () => {
    renderApp(`/models/${EXTRA_MODEL_IDS.fields}/versions/1/explore?section=fields`);
    const viewer = await screen.findByTestId("field-viewer", undefined, T);
    const nav = screen.getByRole("navigation", { name: "Model Explorer sections" });
    expect(nav.querySelector('[data-section="fields"]')).toHaveAttribute("data-status", "ok");
    expect(nav.querySelector('[data-section="fit"]')).toHaveAttribute("data-status", "skipped");

    const stats = within(viewer).getByRole("group", { name: "Statistic" });
    expect(within(stats).getByRole("button", { name: "Mean" })).toHaveAttribute("aria-pressed", "true");
    const frame = within(viewer).getByRole("slider", { name: /period/ });
    expect(frame).toHaveAttribute("aria-valuetext", "period 2019");

    const map = within(viewer).getByRole("img", { name: /^Mean of intensity/ });
    map.focus();
    await userEvent.keyboard("{ArrowRight}");
    const readout = within(viewer).getByTestId("field-readout");
    expect(readout).toHaveTextContent("90% interval");
    expect(readout).toHaveTextContent(/row\s*16/);

    await userEvent.click(within(stats).getByRole("button", { name: "Draws" }));
    expect(within(viewer).getByText("1 of 10")).toBeInTheDocument();
    const play = within(viewer).getByRole("button", { name: "Play the draws" });
    await userEvent.click(play);
    expect(within(viewer).getByRole("button", { name: "Stop" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(viewer).getByRole("button", { name: "Stop" }));

    await userEvent.selectOptions(screen.getByLabelText("Output"), "1");
    const vector = await screen.findByTestId("field-viewer");
    expect(within(vector).getByLabelText("Show the arrows")).toBeChecked();
    expect(within(vector).queryByRole("slider", { name: /period/ })).toBeNull();
  });
});

describe("field viewer in data mode (images of the Data Explorer)", () => {
  it("shows one statistic, the mask and a frame strip that follows the slider", async () => {
    const img = scienceExample("images").fields[0]!;
    render(<FieldViewer field={img} mode="data" mask={img.mask} maskLabel={img.mask_var} frameStats={img.frame_stats} />);
    const viewer = screen.getByTestId("field-viewer");
    // no statistic switch, no draws
    expect(within(viewer).queryByRole("group", { name: "Statistic" })).toBeNull();
    expect(within(viewer).queryByRole("button", { name: "Draws" })).toBeNull();
    expect(within(viewer).getByText("The value of each cell in the data.")).toBeInTheDocument();
    // the mask: drawn and switchable
    expect(within(viewer).getByTestId("field-mask")).toBeInTheDocument();
    const shade = within(viewer).getByLabelText("Shade the cells outside the mask (visible)");
    expect(shade).toBeChecked();
    // the strip marks the frame shown and moves with the slider
    const strip = within(viewer).getByTestId("frame-strip");
    const marker = () => within(strip).getByTestId("frame-strip-marker");
    expect(marker()).toHaveAttribute("x1", "0");
    const slider = within(viewer).getByRole("slider", { name: /quarter/ });
    fireEvent.change(slider, { target: { value: "1" } });
    expect(slider).toHaveAttribute("aria-valuetext", "quarter 2009-04-01");
    expect(marker()).toHaveAttribute("x1", "400");
    // the readout: "Value" and the mask of the cell (row 1, column 1 is outside the mask)
    const map = within(viewer).getByRole("img", { name: /^Value of quakes/ });
    map.focus();
    await userEvent.keyboard("{ArrowDown}");
    const readout = within(viewer).getByTestId("field-readout");
    expect(readout).toHaveTextContent("Value");
    expect(readout).toHaveTextContent(/visible\s*outside/);
    await userEvent.click(shade);
    expect(shade).not.toBeChecked();
  });

  it("gives the colours to 98% of the values when a few cells are much brighter (E58 camera frames)", async () => {
    const img = scienceExample("images").fields[0]!;
    // a 20 x 20 frame with one bright cell
    const spots = Array.from({ length: 400 }, (_, i) => (i === 0 ? 40_000 : 1000 + (i % 7)));
    const bright = { ...img, width: 20, height: 20, frames: [{ ...img.frames[0]!, mean: encodeRaster(spots) }] };
    render(<FieldViewer field={bright} mode="data" />);
    const full = screen.getByLabelText("Colours for the full range");
    expect(full).not.toBeChecked();
    const bar = screen.getByRole("figure", { name: /^Colour scale/ });
    expect(bar).toHaveTextContent("98% of the values");
    expect(bar).not.toHaveTextContent("40,000");
    await userEvent.click(full);
    expect(screen.getByRole("figure", { name: /^Colour scale/ })).toHaveTextContent("40,000");
    // the image of the fixture has no such cells: no check box
    render(<FieldViewer field={img} mode="data" />);
    expect(screen.getAllByTestId("field-viewer")).toHaveLength(2);
    expect(screen.getAllByLabelText("Colours for the full range")).toHaveLength(1);
  });

  it("keeps the model mode unchanged when no mode is given", () => {
    const img = scienceExample("images").fields[0]!;
    render(<FieldViewer field={img} />);
    expect(screen.getByRole("group", { name: "Statistic" })).toBeInTheDocument();
    expect(screen.queryByTestId("field-mask")).toBeNull();
    expect(screen.queryByTestId("frame-strip")).toBeNull();
  });
});
