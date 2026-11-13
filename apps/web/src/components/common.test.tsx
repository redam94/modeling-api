/**
 * The page furniture every page shares: the page header with its breadcrumb trail, and the
 * empty state.
 */
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { EmptyArt } from "./brand/empty-art";
import { EmptyState, PageHeader } from "./common";

/** Render inside a router: breadcrumb links are router links. */
async function renderAt(ui: ReactNode) {
  const root = createRootRoute({ component: () => <>{ui}</> });
  const index = createRoute({ getParentRoute: () => root, path: "/" });
  const models = createRoute({ getParentRoute: () => root, path: "/models" });
  const model = createRoute({ getParentRoute: () => root, path: "/models/$modelId" });
  const router = createRouter({ routeTree: root.addChildren([index, models, model]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(<RouterProvider router={router as never} />);
  await screen.findByRole("heading", { level: 1 });
}

describe("PageHeader", () => {
  it("shows the breadcrumb trail above the title, and marks the last item as the current page", async () => {
    await renderAt(
      <PageHeader
        title="Version 3"
        crumbs={[{ label: "Models", to: "/models" }, { label: "Weekly sales", to: "/models/$modelId", params: { modelId: "mdl_1" } }, { label: "Version 3" }]}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    const items = within(within(nav).getByRole("list")).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(["Models", "Weekly sales", "Version 3"]);
    expect(within(nav).getByRole("link", { name: "Models" })).toHaveAttribute("href", "/models");
    expect(within(nav).getByRole("link", { name: "Weekly sales" })).toHaveAttribute("href", "/models/mdl_1");
    // the current page is text, not a link
    expect(within(nav).queryByRole("link", { name: "Version 3" })).toBeNull();
    expect(within(nav).getByText("Version 3")).toHaveAttribute("aria-current", "page");
    expect(within(nav).getAllByRole("link").every((a) => !a.hasAttribute("aria-current"))).toBe(true);
    // above the title: the trail comes first in the header
    const header = screen.getByRole("heading", { level: 1, name: "Version 3" }).closest("header")!;
    expect(header.querySelector("nav, h1")).toBe(nav);
    expect(nav.querySelector("ol")!.className).toContain("kicker");
  });

  it("stays as it was without a trail: the title, the description, the meta row and the actions", async () => {
    await renderAt(<PageHeader title="Runs" eyebrow="Everything that ran" description="All the runs." meta={<span>12 runs</span>} actions={<button type="button">New run</button>} />);
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Runs" })).toBeInTheDocument();
    expect(screen.getByText("Everything that ran").className).toContain("kicker");
    expect(screen.getByText("All the runs.")).toBeInTheDocument();
    expect(screen.getByText("12 runs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New run" })).toBeInTheDocument();
  });
});

/** The element tree as plain data: tag, class, own text and children. */
function tree(el: Element): unknown {
  const text = Array.from(el.childNodes)
    .filter((n) => n.nodeType === Node.TEXT_NODE)
    .map((n) => n.textContent)
    .join("");
  return { tag: el.tagName.toLowerCase(), class: el.getAttribute("class"), text, children: Array.from(el.children).map(tree) };
}

describe("EmptyState", () => {
  it("is a block without a border on the opposite surface, 28px corners", () => {
    const { container } = render(<EmptyState title="No runs yet">Fit a model to see it here.</EmptyState>);
    const block = container.firstElementChild!;
    expect(block.className).toContain("rounded-3xl");
    expect(block.className).toContain("bg-card");
    // inside a card the block takes the page colour: the opposite surface again
    expect(block.className).toContain("in-data-[slot=card]:bg-background");
    expect(block.className).not.toMatch(/(?:^|\s)border(?:\s|$)|border-dashed/);
    expect(screen.getByRole("heading", { level: 3, name: "No runs yet" })).toBeInTheDocument();
  });

  it("keeps the plain text-only block when no slot is used", () => {
    const { container } = render(
      <EmptyState title="No runs yet" action={<button type="button">Start</button>}>
        Fit a model to see it here.
      </EmptyState>,
    );
    expect(tree(container.firstElementChild!)).toEqual({
      tag: "div",
      class: "flex flex-col items-start gap-2 rounded-3xl bg-card p-8 in-data-[slot=card]:bg-background",
      text: "",
      children: [
        { tag: "h3", class: "font-semibold", text: "No runs yet", children: [] },
        { tag: "div", class: "max-w-[60ch] text-muted-foreground", text: "Fit a model to see it here.", children: [] },
        { tag: "div", class: "pt-2", text: "", children: [{ tag: "button", class: null, text: "Start", children: [] }] },
      ],
    });
  });

  it("puts the art first, then the words, and the primary action before the others", () => {
    render(
      <EmptyState
        title="Your first dashboard starts from a finished run."
        illustration={<EmptyArt kind="dashboards" />}
        primaryAction={<button type="button">Build one</button>}
        action={<button type="button">See an example</button>}
      >
        Pick any analysis.
      </EmptyState>,
    );
    const block = document.querySelector("[data-slot=empty-state]")!;
    const art = block.querySelector("[data-slot=brand-art]")!;
    expect(art).toHaveAttribute("aria-hidden", "true");
    expect(art).toHaveAttribute("alt", "");
    const title = screen.getByRole("heading", { name: "Your first dashboard starts from a finished run." });
    expect(art.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(title.className).toContain("display-title");
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Build one", "See an example"]);
  });

  it("shows a primary action on the plain block too", () => {
    render(<EmptyState title="Nothing here" primaryAction={<button type="button">Add</button>} />);
    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
    expect(document.querySelector("[data-slot=empty-state]")).toBeNull();
  });
});
