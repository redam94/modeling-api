import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { PROJECT_POLICY_ID, VALIDATION_AWAITING, VALIDATION_FAILED } from "@/mocks/governance";
import { createHandlers } from "@/mocks/handlers";
import { PERSONAS } from "@/mocks/tenancy";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const seen: { method: string; path: string; body: unknown }[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    const clone = request.clone();
    void clone.text().then((text) => {
      let body: unknown = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = text;
      }
      seen.push({ method: request.method, path: new URL(request.url).pathname, body });
    });
  });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  seen.length = 0;
  server.resetHandlers(...createHandlers());
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Governance", () => {
  it("lists the policies in force, tenant-wide first, and the check catalog with About cards", async () => {
    const user = userEvent.setup();
    renderApp("/governance");
    const table = await screen.findByRole("table", { name: "Policies" });
    expect(await within(table).findByRole("link", { name: "Forecasts we publish" })).toBeInTheDocument();
    expect(within(table).getAllByText("tenant-wide")).toHaveLength(3);
    await user.click(screen.getByRole("tab", { name: "Check catalog" }));
    const catalog = await screen.findByTestId("check-catalog");
    expect(within(catalog).getAllByTestId("about-card")).toHaveLength(12);
    expect(catalog).toHaveTextContent("Better than shuffled data");
  });

  it("saves a new policy version with the edited checks and the expected version", async () => {
    const user = userEvent.setup();
    renderApp(`/governance/policies/${PROJECT_POLICY_ID}`);
    const editor = await screen.findByTestId("policy-editor");
    await within(editor).findByRole("option", { name: "No effect from false drivers (null.noise_regressors)" });
    await user.selectOptions(within(editor).getByLabelText("Add a check"), "null.noise_regressors");
    await user.click(within(editor).getByRole("button", { name: "Add check" }));
    const holdout = editor.querySelector('li[data-check="holdout.accuracy"]') as HTMLElement;
    const fail = within(holdout).getByLabelText("Fail above");
    await user.clear(fail);
    await user.type(fail, "0.05");
    await user.click(within(editor).getByRole("button", { name: "Save version 3" }));
    // The thresholds break the rule: nothing is sent.
    expect(await within(holdout).findByText(/The fail limit is less than the warn limit/)).toBeInTheDocument();
    expect(seen.some((r) => r.method === "PUT")).toBe(false);
    await user.clear(fail);
    await user.type(fail, "0.3");
    await user.click(within(editor).getByRole("switch", { name: "Require approval by a reviewer" }));
    await user.click(within(editor).getByRole("button", { name: "Save version 3" }));
    await waitFor(() => expect(seen.some((r) => r.method === "PUT")).toBe(true));
    const put = seen.find((r) => r.method === "PUT")!;
    expect(put.path).toBe(`/v1/governance/policies/${PROJECT_POLICY_ID}`);
    expect(put.body).toMatchObject({ expected_version: 2, require_approval: false });
    const body = put.body as { checks: { check: string; thresholds?: { warn: number; fail: number } }[] };
    expect(body.checks.map((c) => c.check)).toContain("null.noise_regressors");
    expect(body.checks.find((c) => c.check === "holdout.accuracy")?.thresholds).toEqual({ warn: 0.1, fail: 0.3 });
  });

  it("lets a reviewer approve a colleague's validation, with the evidence beside it", async () => {
    const user = userEvent.setup();
    renderApp(`/validations/${VALIDATION_AWAITING}`);
    const view = await screen.findByTestId("validation-view");
    expect(within(view).getByRole("list", { name: "Validation progress" })).toHaveTextContent("Awaiting approval");
    expect(view.querySelector('article[data-check="null.permutation_target"]')).toHaveTextContent("than all 19 fits with the drivers price shuffled");
    const form = within(view).getByTestId("decision-form");
    await user.click(within(form).getByRole("button", { name: "Reject" }));
    expect(within(form).getByText("The comment is empty. Type the reason for the rejection.")).toBeInTheDocument();
    expect(seen.some((r) => r.path.endsWith("/approvals"))).toBe(false);
    await user.type(within(form).getByLabelText("Comment"), "Convincing null tests.");
    await user.click(within(form).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(within(view).getAllByText("Validated").length).toBeGreaterThan(0));
    expect(seen.find((r) => r.path.endsWith("/approvals"))?.body).toEqual({ decision: "approve", comment: "Convincing null tests." });
  });

  it("hides decisions from members without the reviewer capability", async () => {
    renderApp(`/validations/${VALIDATION_AWAITING}`, "rui");
    const view = await screen.findByTestId("validation-view");
    expect(within(view).getByRole("heading", { name: "Approvals" })).toBeInTheDocument();
    expect(within(view).queryByTestId("decision-form")).toBeNull();
  });

  it("offers a resubmission after failed checks and shows what to do", async () => {
    const user = userEvent.setup();
    renderApp(`/validations/${VALIDATION_FAILED}`);
    const view = await screen.findByTestId("validation-view");
    expect(view).toHaveTextContent("blocking: null.permutation_target, baseline.beats_naive");
    expect(view).toHaveTextContent("What to do:");
    await user.click(within(view).getByRole("button", { name: "Resubmit" }));
    const dialog = await screen.findByRole("dialog", { name: "Run the checks again?" });
    await user.click(within(dialog).getByRole("button", { name: "Resubmit" }));
    await waitFor(() => expect(within(view).getAllByText("Checks running").length).toBeGreaterThan(0));
  });

  it("requests a validation held to every policy in force; ticking a policy only adds it", async () => {
    const user = userEvent.setup();
    renderApp("/models/mdl_01jaf3c1d2e3f4g5h6j7k8m9n0/versions/1/validations");
    const form = await screen.findByRole("form", { name: "Request a validation" });
    expect(within(form).getByTestId("policies-in-force-note")).toHaveTextContent("All policies in force for this type of model apply.");
    const boxes = await within(form).findAllByRole("checkbox");
    expect(boxes.length).toBeGreaterThan(0);
    for (const box of boxes) expect(box).not.toBeChecked();
    await user.click(within(form).getByRole("button", { name: "Request validation" }));
    await waitFor(() => expect(seen.some((r) => r.method === "POST" && r.path.endsWith("/validations"))).toBe(true));
    const post = seen.find((r) => r.method === "POST" && r.path.endsWith("/validations"))!;
    expect(post.body).toMatchObject({ policy_ids: [] });
  });

  it("grants the reviewer capability on Members", async () => {
    const user = userEvent.setup();
    renderApp("/members");
    const toggle = await screen.findByRole("switch", { name: "Rui Runner can approve validations" });
    await user.click(toggle);
    await waitFor(() => expect(seen.some((r) => r.method === "PATCH")).toBe(true));
    const patch = seen.find((r) => r.method === "PATCH")!;
    expect(patch.path).toBe(`/v1/tenants/acme/projects/growth/members/${PERSONAS.rui!.id}`);
    expect(patch.body).toEqual({ capabilities: ["reviewer"] });
  });
});
