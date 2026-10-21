import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/overlays";

import type { ClientDashboard } from "../types";
import { resetUpgradeNoteMemory } from "./panel-model";
import { PanelReadout } from "./panel-readout";
import { classifierDashboard, fakeClient, readout, snapshot } from "./panels/test-fixtures";
import { ReadoutPage } from "./readout-page";

function view(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  );
}

const created: string[] = [];
beforeEach(() => {
  resetUpgradeNoteMemory();
  sessionStorage.clear();
  created.length = 0;
  URL.createObjectURL = vi.fn(() => {
    const u = `blob:test/${created.length}`;
    created.push(u);
    return u;
  });
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  // jsdom has no canvas: the field map keeps its accessible name and table without pixels
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => null);
});

/** A File with `text()` (jsdom's File has none). */
function csvFile(text: string, name = "cases.csv"): File {
  return Object.assign(new File([text], name, { type: "text/csv" }), { text: () => Promise.resolve(text) });
}
afterEach(() => vi.restoreAllMocks());

describe("PanelReadout: Overview", () => {
  it("lays out the KPI tiles, then a card per panel, each heavy chart with numbers in its name and a hidden table", async () => {
    const d = classifierDashboard();
    const client = fakeClient(d);
    view(<PanelReadout client={client} dashboard={d} />);
    expect(screen.getByRole("heading", { level: 1, name: "Churn risk" })).toBeInTheDocument();
    expect(document.querySelector("[data-readout=lead]")?.textContent).toBe("The model gives a 30% chance of churn for this case.");
    const tabs = screen.getByRole("tablist", { name: "Dashboard sections" });
    expect(within(tabs).getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview", "Try a case", "Check accuracy", "Score a file", "History"]);

    const tiles = screen.getByRole("list", { name: "Key numbers" });
    const items = within(tiles).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]!).getByText("30.0%")).toBeInTheDocument();
    expect(within(items[0]!).getByText(/^Likely between 20\.0% and 40\.0%\./)).toBeInTheDocument();
    expect(within(items[0]!).getByText(/^10% more than the readout before$/)).toBeInTheDocument();
    expect(within(items[1]!).getByText(/Approximate\./)).toBeInTheDocument();

    // heavy blocks load lazily
    expect(await screen.findByRole("img", { name: /^Probability of each result: 2 bars\. Highest no 70\.0%\./ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Probability of each result" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^Predicted and actual: 3 points\./ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^Calibration: 2 groups of cases\. The largest gap: the model said 80%, and the result occurred in 60% of 10 cases\./ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /^Right and wrong: 12 of 15 cases were predicted right\./ })).toBeInTheDocument();
    const map = screen.getByRole("img", { name: /^Risk map, 2026: a map of 2 by 2 cells\. Values from 0 cases to 10 cases; the average is 5 cases\.$/ });
    expect(map.tagName).toBe("CANVAS");
    fireEvent.change(screen.getByRole("slider"), { target: { value: "1" } });
    expect(screen.getByRole("img", { name: /^Risk map, 2027:/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Next cases" })).toBeInTheDocument();
    expect(screen.getByText(/^Line one\.\s+Line two\.$/)).toBeInTheDocument();
    expect(screen.getByText("This model gives no feature importance.")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^Two lines: 2 lines over 2 cases to 01\/02\/2026\. Latest: A 2\.00, B 1\.00\.$/ })).toBeInTheDocument();
    // the picture: fetched through the client as a Blob, shown from a blob: URL
    const pic = await screen.findByRole("img", { name: "The loss falls over 20 epochs." });
    expect(pic.getAttribute("src")).toMatch(/^blob:/);
    expect(client.calls.find((c) => c.method === "asset")?.args[0]).toBe("dsa_01jaf3w1abcdefghjkmnpqrstv");
    // the approximate tag on the card of an approximate block
    const card = document.querySelector("[data-panel=expected]");
    expect(card).not.toBeNull();
  });

  it("hides sections the client cannot use and a readout that is not ready says so", () => {
    const d = classifierDashboard({ sections: ["overview", "history"], latest_readout: readout(4, { status: "running", snapshot: null }) });
    view(<PanelReadout client={fakeClient(d)} dashboard={d} />);
    expect(within(screen.getByRole("tablist")).getAllByRole("tab").map((t) => t.textContent)).toEqual(["Overview", "History"]);
    expect(screen.getByText(/^Calculating\. The readout appears here when it is ready\.$/)).toBeInTheDocument();
  });

  it("an old layout 1 readout on an upgraded dashboard still shows its tiles", () => {
    const d = classifierDashboard({
      latest_readout: readout(2, { snapshot: { lead: "Old lead.", tiles: [{ key: "expected", label: "Expected churn", value: 3, unit: "count" }], series: [], channels: [] } }),
    });
    view(<PanelReadout client={fakeClient(d)} dashboard={d} />);
    expect(screen.getByText("Old lead.")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Key numbers" })).getByText("Expected churn")).toBeInTheDocument();
  });
});

describe("the one-time upgrade note", () => {
  const note = "This dashboard has a new layout. The numbers come from the same model.";

  it("a client user closes it once; it stays closed in this browser", async () => {
    const d = classifierDashboard({ upgrade_note: note });
    const first = view(<PanelReadout client={fakeClient(d)} dashboard={d} />);
    expect(screen.getByText(note)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText(note)).toBeNull();
    expect(localStorage.getItem("mapi.dashboards.upgradeNote.dsh_classifier")).toBe(note);
    first.unmount();
    resetUpgradeNoteMemory();
    view(<PanelReadout client={fakeClient(d)} dashboard={d} />);
    expect(screen.queryByText(note)).toBeNull();
    // a new note text shows again
    localStorage.setItem("mapi.dashboards.upgradeNote.dsh_classifier", "an older note");
    resetUpgradeNoteMemory();
    view(<PanelReadout client={fakeClient(d)} dashboard={d} />);
    expect(screen.getByText(note)).toBeInTheDocument();
  });

  it("a share link keeps it for the session only, never in local storage", async () => {
    const d = classifierDashboard({ upgrade_note: note });
    view(<PanelReadout client={fakeClient(d, { kind: "share" })} dashboard={d} />);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(localStorage.getItem("mapi.dashboards.upgradeNote.dsh_classifier")).toBeNull();
    expect(sessionStorage.getItem("mapi.dashboards.upgradeNote.dsh_classifier")).toBe(note);
  });

  it("works when browser storage throws, and the preview never shows it", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const d = classifierDashboard({ upgrade_note: note });
    const v = view(<PanelReadout client={fakeClient(d)} dashboard={d} />);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText(note)).toBeNull();
    v.unmount();
    view(<PanelReadout client={fakeClient(d)} dashboard={d} preview />);
    expect(screen.queryByText(note)).toBeNull();
  });

  it("the media planner keeps its tabs, with the note and the extra panels", async () => {
    const d = classifierDashboard({
      preset: "mmm_planner",
      period_label: "week",
      tabs: ["this_period", "history"],
      upgrade_note: note,
      latest_readout: readout(5, { snapshot: snapshot({ preset: "mmm_planner", lead: "Media brought 10 revenue this week.", panels: [{ kind: "note", panel_id: "n", title: "Analyst note", text: "Hello." }] }) }),
    });
    view(<PanelReadout client={fakeClient(d)} dashboard={d} />);
    expect(within(screen.getByRole("tablist")).getAllByRole("tab").map((t) => t.textContent)).toEqual(["This week", "History"]);
    expect(screen.getByText(note)).toBeInTheDocument();
    expect(await screen.findByText("Hello.")).toBeInTheDocument();
  });
});

describe("ReadoutPage picks the layout", () => {
  it("renders the panels for a layout 2 dashboard read through the client", async () => {
    const d = classifierDashboard();
    view(<ReadoutPage client={fakeClient(d)} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Churn risk" })).toBeInTheDocument();
    expect(document.querySelector("[data-layout='2']")).not.toBeNull();
  });

  it("maps a layout 1 ?tab= to the section", async () => {
    const d = classifierDashboard();
    view(<ReadoutPage client={fakeClient(d)} tab="history" />);
    expect(await screen.findByRole("list", { name: "Readouts" })).toBeInTheDocument();
  });
});

describe("Try a case", () => {
  function open(d: ClientDashboard, client = fakeClient(d)) {
    view(<PanelReadout client={client} dashboard={d} section="try" />);
    return client;
  }

  it("marks a value out of range at its field before sending, then shows the prediction and keeps it", async () => {
    const d = classifierDashboard();
    const client = open(d);
    const tenure = await screen.findByRole("spinbutton", { name: "Months as a customer" });
    const calc = screen.getByRole("button", { name: "Calculate" });
    expect(calc).toBeEnabled();
    fireEvent.change(tenure, { target: { value: "200" } });
    expect(tenure).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Months as a customer must be between 0 and 120.")).toBeInTheDocument();
    expect(calc).toBeDisabled();
    fireEvent.change(tenure, { target: { value: "24" } });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Plan" }), "pro");
    await userEvent.click(calc);
    const result = await screen.findByRole("list", { name: "Predictions" });
    expect(within(result).getByText("no")).toBeInTheDocument();
    expect(within(result).getByText(/^Probability 58\.0%\. Likely between 30\.0% and 55\.0%\.$/)).toBeInTheDocument();
    expect(screen.getByText("The range shows the average, not single results.")).toBeInTheDocument();
    expect(client.calls.find((c) => c.method === "predict")?.args[0]).toEqual({ rows: [{ tenure: 24, plan: "pro" }] });
    expect(calc).toBeDisabled(); // the same case twice would score twice
    await userEvent.click(screen.getByRole("button", { name: "Keep this case" }));
    expect(await screen.findByText("Kept. The analyst checks it before other people see it.")).toBeInTheDocument();
  });

  it("shows the server's refusal at the field of its pointer; a share link has no keep", async () => {
    const d = classifierDashboard();
    const problem = { type: "about:blank", title: "Input invalid", status: 422, code: "DASHBOARD_INPUT_INVALID", errors: [{ code: "DASHBOARD_INPUT_INVALID", pointer: "/rows/0/plan", message: "Plan must be one of the choices" }] };
    const client = fakeClient(d, { kind: "share", onPredict: () => Promise.reject(Object.assign(new Error("Input invalid"), { status: 422, problem })) });
    open(d, client);
    await userEvent.click(await screen.findByRole("button", { name: "Calculate" }));
    expect(await screen.findByText("Plan must be one of the choices.")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Plan" })).toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByRole("button", { name: "Keep this case" })).toBeNull();
  });
});

describe("Score a file", () => {
  it("previews and checks the file, scores it and downloads the predictions through the client", async () => {
    const d = classifierDashboard();
    const client = fakeClient(d);
    const { container } = view(<PanelReadout client={client} dashboard={d} section="batch" />);
    await screen.findByRole("button", { name: /Drop a \.csv file here/ });
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    await userEvent.upload(input, csvFile("case_id,tenure,plan\na,12,basic\nb,200,pro\n"));
    expect(await screen.findByRole("table", { name: "The first rows of cases.csv" })).toBeInTheDocument();
    const checks = screen.getByRole("list", { name: "Checks" });
    expect(within(checks).getByText("Fix the values. row 2: tenure must be between 0 and 120.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Score the file" })).toBeDisabled();

    await userEvent.upload(input, csvFile("case_id,tenure,plan\na,12,basic\nb,20,pro\n"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Score the file" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Score the file" }));
    expect(await screen.findByText("The model scored 2 of 2 cases.")).toBeInTheDocument();
    expect(screen.getByText('50.0% of the cases are likely "yes".')).toBeInTheDocument();
    expect(screen.getByText(/^churn: the middle prediction is 40\.0%; most are between 12\.0% and 70\.0%\. This is the spread of the predictions, not a likely range\.$/)).toBeInTheDocument();
    const sent = client.calls.find((c) => c.method === "submitBatch")?.args[0] as { inputs: unknown };
    expect(sent.inputs).toEqual({ coords: { case_id: { dims: ["case_id"], data: ["a", "b"] } }, data_vars: { tenure: { dims: ["case_id"], data: [12, 20] }, plan: { dims: ["case_id"], data: ["basic", "pro"] } } });
    await userEvent.click(screen.getByRole("button", { name: "Download predictions (.csv)" }));
    await waitFor(() => expect(client.calls.some((c) => c.method === "downloadPredictions")).toBe(true));
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled());
  });
});

describe("Check accuracy", () => {
  it("row match: pasted cases are checked, sent, and the server's accuracy is shown", async () => {
    const d = classifierDashboard({
      latest_readout: readout(3, { snapshot: snapshot({ accuracy_history: [{ seq: 1, created_at: "2026-09-01T00:00:00Z", values: { accuracy: 0.7 } }, { seq: 2, created_at: "2026-10-01T00:00:00Z", values: { accuracy: 0.8 } }] }) }),
    });
    const client = fakeClient(d);
    view(<PanelReadout client={client} dashboard={d} section="accuracy" />);
    expect(await screen.findByRole("img", { name: /^Right predictions over time: 1 lines over 2/ })).toBeInTheDocument();
    await userEvent.click(await screen.findByText("Paste from a spreadsheet"));
    fireEvent.change(screen.getByRole("textbox", { name: "Rows with the column names first" }), { target: { value: "case_id,tenure,churned\nc1,12,yes\nc2,3,no\n" } });
    await userEvent.click(screen.getByRole("button", { name: "Use the pasted rows" }));
    const checks = screen.getByRole("list", { name: "Checks" });
    expect(within(checks).getByText("2 cases to send.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText(/^We received the results\./)).toBeInTheDocument();
    expect(await screen.findByText("The model was right 50.0% of the time.")).toBeInTheDocument();
    const body = client.calls.find((c) => c.method === "submitActuals")?.args[0] as { inputs: { coords: Record<string, unknown> } };
    expect(Object.keys(body.inputs.coords)).toEqual(["case_id"]);
  });
});

describe("History", () => {
  it("draws each readout by its own shape", async () => {
    const d = classifierDashboard();
    const client = fakeClient(d);
    client.readouts_.set(1, readout(1, { title: "Old readout", snapshot: { lead: "Old.", tiles: [{ key: "expected", label: "Expected churn", value: 3, unit: "count" }], series: [], channels: [] } }));
    view(<PanelReadout client={client} dashboard={d} section="history" />);
    const list = await screen.findByRole("list", { name: "Readouts" });
    await waitFor(() => expect(within(list).getAllByRole("button")).toHaveLength(2));
    await userEvent.click(within(list).getByRole("button", { name: /Readout 3/ }));
    const detail = () => document.querySelector("[data-readout=detail]") as HTMLElement;
    expect(await within(detail()).findByRole("list", { name: "Key numbers of Readout 3" })).toBeInTheDocument();
    await userEvent.click(within(list).getByRole("button", { name: /Old readout/ }));
    expect(await within(detail()).findByText("Expected churn")).toBeInTheDocument();
  });
});
