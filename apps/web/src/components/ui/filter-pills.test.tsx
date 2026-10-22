import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FilterPills } from "./filter-pills";

const OPTIONS = [
  { value: "active", label: "Active" },
  { value: "draft", label: "Drafts" },
];

describe("FilterPills (v0.18 Studio pills for Dashboards, Help me choose, Settings)", () => {
  it("is a single-choice group of pills with the chosen one on", async () => {
    const onValueChange = vi.fn();
    render(<FilterPills label="Status" value="active" onValueChange={onValueChange} options={OPTIONS} />);
    expect(screen.getByRole("radiogroup", { name: "Status" }).className).not.toContain("bg-border");
    const on = screen.getByRole("radio", { name: "Active" });
    expect(on).toHaveAttribute("data-state", "on");
    expect(on.className).toContain("filter-pill");
    expect(screen.getByRole("radio", { name: "Drafts" })).toHaveAttribute("data-state", "off");
    await userEvent.click(screen.getByRole("radio", { name: "Drafts" }));
    expect(onValueChange).toHaveBeenCalledWith("draft");
  });

  it("is one tab stop; the arrow keys move between the pills and Enter chooses", async () => {
    const onValueChange = vi.fn();
    render(<FilterPills label="Status" value="active" onValueChange={onValueChange} options={OPTIONS} />);
    expect(screen.getByRole("radio", { name: "Active" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Drafts" })).toHaveAttribute("tabindex", "-1");
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole("radio", { name: "Active" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Drafts" })).toHaveFocus();
    expect(onValueChange).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(onValueChange).toHaveBeenCalledWith("draft");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Active" })).toHaveFocus();
  });

  it("never clears the choice when the chosen pill is pressed again", async () => {
    const onValueChange = vi.fn();
    render(<FilterPills label="Status" value="active" onValueChange={onValueChange} options={OPTIONS} />);
    await userEvent.click(screen.getByRole("radio", { name: "Active" }));
    expect(onValueChange).not.toHaveBeenCalled();
  });
});
