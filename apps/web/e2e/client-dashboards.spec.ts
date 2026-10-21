/**
 * v0.16 client dashboards against the MSW-mocked API (`vite --mode mock`, production build with
 * the CSP and Trusted Types enforced): the analyst publishes a draft, gives Cleo (a client user)
 * access and creates a passcode link; Cleo sees only the portal, a slider changes the plan, a
 * CSV upload with a refit request lands in History waiting for review; the analyst approves a
 * client's refit request; the portal holds up at 390 px and in the dark theme.
 *
 * The mock keeps its state in memory per page load and a persona switch reloads the page, so a
 * flow across two people uses the mock's fixtures for the other side (Cleo's open refit request
 * on readout 13 is the one the analyst decides).
 *
 * Screenshots of the portal land in the test output folder (and in MAPI_SHOTS_DIR when set).
 */
import { expect, test, type Page, type TestInfo } from "@playwright/test";

const MMM = "dsh_01jaf3w1mmm2weekly3x4y5z6";
const DRAFT = "dsh_01jaf3w1promo2draft3x4y5z";
const SHOTS = process.env.MAPI_SHOTS_DIR;

async function as(page: Page, persona: "ana" | "rui" | "vic" | "cleo") {
  await page.addInitScript((p) => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    localStorage.setItem("mapi.mock.session", p);
    // Queued readouts become ready quickly.
    localStorage.setItem("mapi.mock.readoutMs", "300");
    sessionStorage.setItem("mapi.test.persona", "1");
  }, persona);
}

async function shot(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

async function noHorizontalScroll(page: Page) {
  const { scroll, client, wide } = await page.evaluate(() => {
    const client = document.documentElement.clientWidth;
    // The outermost elements that reach past the right edge (named in the failure message).
    const wide = [...document.querySelectorAll<HTMLElement>("main *")]
      .filter((el) => el.getBoundingClientRect().right > client + 1 && !(el.parentElement && el.parentElement.getBoundingClientRect().right > client + 1))
      .slice(0, 5)
      .map((el) => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 80)} [${el.getAttribute("aria-label") ?? el.dataset.readout ?? ""}] right=${Math.round(el.getBoundingClientRect().right)}`);
    return { scroll: document.documentElement.scrollWidth, client, wide };
  });
  expect(scroll, `wider than ${client} px: ${wide.join(" | ")}`).toBeLessThanOrEqual(client);
}

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
});

test.afterEach(async ({ page }) => {
  const violations = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
  expect(violations).toEqual([]);
});

test("the analyst publishes a draft, gives a client access and creates a passcode link", async ({ page }) => {
  await as(page, "ana");
  await page.goto("/dashboards");
  const list = page.getByRole("table", { name: "Dashboards" });
  await expect(list.getByText("Weekly media readout")).toBeVisible();
  await list.getByText("Spring promotion plan").click();
  await expect(page).toHaveURL(new RegExp(`/dashboards/${DRAFT}`));
  await expect(page.getByRole("heading", { name: "Spring promotion plan" })).toBeVisible();

  // Publish: the dialog checks the draft and says what publishing costs.
  await page.getByRole("button", { name: /^Publish$/ }).click();
  const publish = page.getByRole("dialog", { name: /Publish “Spring promotion plan”/ });
  await expect(publish.getByTestId("publish-cost")).toContainText("Live inputs run as you and use your project's compute.");
  await expect(publish.getByText("The draft is ready to publish.")).toBeVisible({ timeout: 15_000 });
  await publish.getByRole("button", { name: /^Publish$/ }).click();
  await expect(publish).toBeHidden();
  await expect(page.getByText(/Revision 1 published/)).toBeVisible({ timeout: 15_000 });

  // Access: give Cleo access.
  await page.getByRole("tab", { name: "Access" }).click();
  await page.getByLabel("Client user").selectOption({ label: "Cleo Client (cleo@northwind.example)" });
  await page.getByRole("button", { name: "Give access" }).click();
  const grants = page.getByRole("table", { name: "Clients with access" });
  await expect(grants.getByText("Cleo Client")).toBeVisible();
  await expect(grants.getByText("cleo@northwind.example")).toBeVisible();

  // A passcode link that may try plan scenarios. Read-only is the default.
  await page.getByRole("button", { name: /Create link/ }).click();
  const create = page.getByRole("dialog");
  await expect(create.getByTestId("share-access-summary")).toHaveText("This link: Read only.");
  await create.getByLabel("Label (optional)").fill("Agency planner");
  await create.getByRole("checkbox", { name: "Let them try plan scenarios" }).click();
  await create.getByLabel("Passcode (optional)").fill("short");
  await create.getByRole("button", { name: /Create link/ }).click();
  await expect(create.getByText("Use at least 8 characters for the passcode.")).toBeVisible();
  await create.getByLabel("Passcode (optional)").fill("harbour-lights-2");
  await create.getByRole("button", { name: /Create link/ }).click();

  const shown = page.getByRole("dialog", { name: "Copy the link now" });
  const link = await shown.getByRole("textbox", { name: "Share link" }).inputValue();
  expect(link).toMatch(/\/s#mpd_[0-9a-hjkmnp-tv-z]{40}$/);
  const token = link.split("#")[1] ?? "";
  await expect(shown.getByRole("button", { name: "Done" })).toBeDisabled();
  await shown.getByText("I have copied the link").click();
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(shown).toBeHidden();

  const shares = page.getByRole("table", { name: "Share links" });
  const row = shares.getByRole("row").filter({ hasText: "Agency planner" });
  await expect(row.getByText(`mpd_…${token.slice(-4)}`)).toBeVisible();
  await expect(row.getByText("With a passcode")).toBeVisible();
  // The token is shown one time only.
  expect(await page.locator("body").textContent()).not.toContain(token);

  // Revoke it: the row says so at once.
  await row.getByRole("button", { name: "Revoke Agency planner" }).click();
  const revoke = page.getByRole("dialog", { name: "Revoke “Agency planner”?" });
  await revoke.getByRole("button", { name: "Revoke link" }).click();
  await expect(revoke).toBeHidden();
  await expect(shares.getByRole("row").filter({ hasText: "Agency planner" }).getByText("revoked", { exact: true })).toBeVisible();
});

test("a client sees only the portal, plans with a slider and sends numbers with a refit request", async ({ page }, info) => {
  await as(page, "cleo");
  await page.goto("/runs");
  // Any path of the full app ends on the portal: no navigation, no command palette.
  await expect(page.getByRole("heading", { level: 1, name: "Your dashboards" })).toBeVisible();
  await expect(page).toHaveURL(/\/c$/);
  await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
  const dashboards = page.getByRole("list", { name: "Dashboards" });
  await expect(dashboards.getByRole("listitem")).toHaveCount(2);
  await dashboards.getByText("Weekly media readout").click();
  await expect(page).toHaveURL(new RegExp(`/c/${MMM}$`));
  await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Key numbers" }).getByRole("listitem")).toHaveCount(4);
  await shot(page, info, "client-portal-readout");

  // Plan scenarios: a slider answers at once, in the browser.
  await page.getByRole("tab", { name: "Plan scenarios" }).click();
  await expect(page).toHaveURL(/\?tab=scenarios$/);
  const lead = page.locator("[data-readout=scenario-lead]");
  await expect(lead).toHaveText("This plan brings the same result as the current plan.");
  const whatifCalls: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("whatif")) whatifCalls.push(r.url());
  });
  const slider = page.getByRole("slider", { name: "Paid search" });
  await slider.focus();
  for (let i = 0; i < 6; i++) await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveAttribute("aria-valuetext", /^\+30% spend/);
  await expect(lead).toHaveText(/^This plan brings [\d,]+ more revenue than the current plan, for [\d,]+ more spend\.$/);
  await expect(page.getByRole("img", { name: /^Change in revenue by channel: .*Paid search \+[\d,]+/ })).toBeVisible();
  expect(whatifCalls).toEqual([]);

  // Update numbers: upload a CSV and ask for a refit.
  await page.getByRole("tab", { name: "Update numbers" }).click();
  await expect(page.getByText("We compare your numbers with what the model expected. The model does not change.")).toBeVisible();
  const csv = [
    "date,revenue,spend_tv,spend_search,spend_social,spend_display",
    "2026-09-27,401000,42000,18000,12000,8000",
    "2026-10-04,402500,42000,18000,12000,8000",
    "2026-10-11,398000,42000,18000,12000,8000",
    "2026-10-18,405000,42000,18000,12000,8000",
  ].join("\n");
  await page.locator("[data-tab=update] input[type=file]").setInputFiles({ name: "northwind-actuals.csv", mimeType: "text/csv", buffer: Buffer.from(`${csv}\n`) });
  await expect(page.getByRole("textbox", { name: "revenue, row 4" })).toHaveValue("405000");
  await page.getByRole("checkbox", { name: "Ask the analyst to refit with these numbers" }).click();
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText(/We received your numbers\. The new readout appears in History when it is ready\. The analyst checks it before other people see it\./)).toBeVisible();
  const sent = page.locator("[data-readout=sent]");
  await expect(sent.getByText("Refit requested")).toBeVisible();
  await expect(sent.getByText("Waiting for review")).toBeVisible();
  await expect(sent.getByText(/^Your numbers were \d+% (more|less) than the model expected over 4 weeks\./)).toBeVisible({ timeout: 10_000 });

  // History: the new upload is there for Cleo, waiting for review, with its refit request.
  await page.getByRole("tab", { name: "History" }).click();
  const today = new Date().toLocaleDateString("en-GB");
  const readouts = page.getByRole("list", { name: "Readouts" });
  const mine = readouts.getByRole("listitem").filter({ hasText: `Numbers sent on ${today}` }).first();
  await expect(mine).toBeVisible();
  await expect(mine.getByText("Refit requested")).toBeVisible();
});

test("the analyst sees a client's refit request and approves it", async ({ page }) => {
  await as(page, "ana");
  await page.goto(`/dashboards/${MMM}?tab=history`);
  const pending = page.getByRole("list", { name: "Readouts waiting for review" });
  await expect(pending.getByText("Numbers sent on 20/10/2026")).toBeVisible({ timeout: 15_000 });
  const refits = page.getByRole("list", { name: "Refit requests" });
  const request = refits.getByRole("listitem").filter({ hasText: "Refit with the numbers of readout 13" });
  await expect(request).toHaveAttribute("data-status", "open");
  await expect(request.getByText(/Asked by a client user/)).toBeVisible();

  await request.getByRole("button", { name: /Approve/ }).click();
  const dialog = page.getByRole("dialog", { name: "Approve the refit?" });
  await expect(dialog.getByText(/starts .* as you/)).toBeVisible();
  await dialog.getByRole("button", { name: /Approve and start the refit/ }).click();
  await expect(dialog).toBeHidden();
  await expect(request).toHaveAttribute("data-status", "approved");
  await expect(request.getByText("Approved")).toBeVisible();
  await expect(request.getByRole("button", { name: /Approve/ })).toHaveCount(0);
  // Approving publishes the readout the refit was asked on: nothing is left to review.
  await expect(pending.getByText("Numbers sent on 20/10/2026")).toHaveCount(0);
});

test("a viewer sees the uploads waiting for review but cannot decide them", async ({ page }) => {
  await as(page, "vic");
  await page.goto(`/dashboards/${MMM}?tab=history`);
  const pending = page.getByRole("list", { name: "Readouts waiting for review" });
  await expect(pending.getByText("Numbers sent on 20/10/2026")).toBeVisible({ timeout: 15_000 });
  await expect(pending.getByRole("button", { name: "Publish readout 13" })).toBeDisabled();
  // Refit requests need the editor role (approving one also needs runner).
  await expect(page.getByText("Seeing refit requests needs the editor role. Your role in acme/growth is viewer.")).toBeVisible();
  await expect(page.getByRole("list", { name: "Refit requests" })).toHaveCount(0);
});

test.describe("the client portal at 390 px and in the dark theme", () => {
  test.use({ viewport: { width: 390, height: 844 }, colorScheme: "dark" });

  test("fits the phone width and uses the dark theme", async ({ page }, info) => {
    await as(page, "cleo");
    await page.goto("/c");
    await expect(page.getByRole("heading", { level: 1, name: "Your dashboards" })).toBeVisible();
    await expect(page.locator("html")).toHaveClass(/\bdark\b/);
    // The page background is dark (a token, not the light default).
    const lum = await page.evaluate(() => {
      const c = getComputedStyle(document.body).backgroundColor;
      const probe = document.createElement("canvas").getContext("2d")!;
      probe.fillStyle = c;
      probe.fillRect(0, 0, 1, 1);
      const [r, g, b] = probe.getImageData(0, 0, 1, 1).data;
      return (0.2126 * r! + 0.7152 * g! + 0.0722 * b!) / 255;
    });
    expect(lum).toBeLessThan(0.25);
    await noHorizontalScroll(page);
    await shot(page, info, "client-portal-list-dark-390");

    await page.getByRole("list", { name: "Dashboards" }).getByText("Weekly media readout").click();
    await expect(page.getByRole("heading", { level: 1, name: "Weekly media readout" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Channel scorecard" })).toBeVisible();
    await noHorizontalScroll(page);
    await shot(page, info, "client-portal-readout-dark-390");

    for (const tab of ["Trends", "Plan scenarios", "Update numbers", "History"]) {
      await page.getByRole("tab", { name: tab }).click();
      await expect(page.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
      await noHorizontalScroll(page);
    }
    await shot(page, info, "client-portal-history-dark-390");
  });
});
