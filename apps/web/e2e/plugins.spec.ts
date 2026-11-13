/**
 * v0.19 uploaded code on the MSW mock (plan D20, WP10): drop a wheel and follow the checks to
 * the test report and the SBOM; a refused file's problems in plain words; the reviewer panel
 * (facts first, URLs with punycode, never a `javascript:` link, untrusted text as text, the
 * page's digests in the decision); the uploader's disabled buttons; a stale sign-in through the
 * identity provider and back; withdraw with usage counts; Catalog › Yours › Uploaded with
 * "Only you can use this"; the settings card; 390 px without sideways scrolling.
 *
 * Mock switches (mocks/plugins.ts): `mapi.mock.pluginMs` (how long the fake checks take),
 * `mapi.mock.pluginReauth=1` (the next decision needs a fresh sign-in),
 * `mapi.mock.devUntrusted=1` (no sandbox: the dev banner).
 * `MAPI_SHOTS_DIR` also writes the screenshots the docs use.
 */
import { expect, test, type Page, type TestInfo } from "@playwright/test";

const PLUGIN = "plg_01jbz3q2x8m9v7k5h4g3f2e1a0";
const vid = (n: number) => `plv_01jbz3q2x8m9v7k5h4g3f${n.toString(36).padStart(5, "0")}`;
const V = { approved: vid(1), awaiting: vid(2), awaitingOwn: vid(3), privateOwn: vid(4), kitFailed: vid(5), refused: vid(6) };
const SHOTS = process.env.MAPI_SHOTS_DIR;

async function mock(page: Page, values: Record<string, string>) {
  await page.addInitScript((v) => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.mocked")) return;
    for (const [k, x] of Object.entries(v)) localStorage.setItem(k, x);
    sessionStorage.setItem("mapi.test.mocked", "1");
  }, values);
}

async function noSideScroll(page: Page) {
  const { overflow, wide } = await page.evaluate(() => {
    const w = document.documentElement.clientWidth;
    // the widest element that sticks out names the culprit when this fails
    const out = [...document.querySelectorAll("body *")].filter((el) => el.getBoundingClientRect().right > w + 1);
    const last = out.at(-1);
    return {
      overflow: document.documentElement.scrollWidth - w,
      wide: last ? `${last.tagName.toLowerCase()}.${String(last.className).slice(0, 80)} "${(last.textContent ?? "").slice(0, 40)}"` : "",
    };
  });
  expect(overflow, `${page.url()} ${wide}`).toBeLessThanOrEqual(0);
}

async function shot(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

/** A wheel-shaped file (the mock never opens it). */
function wheel(name: string) {
  return { name, mimeType: "application/zip", buffer: Buffer.alloc(4096, 1) };
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

test("upload a wheel: the checks run, then the test report and the SBOM; Yours shows it as private", async ({ page }, info) => {
  await mock(page, { "mapi.mock.pluginMs": "2500" });
  const posts: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST") posts.push(new URL(r.url()).pathname);
  });
  await page.goto(`/plugins/new?plugin=${PLUGIN}`);
  await expect(page.getByRole("heading", { name: "Upload your own code" })).toBeVisible();
  await expect(page.getByText(/Namespace t_k3vq7m_acme/)).toBeVisible();
  await expect(page.getByTestId("plugin-upload-cli")).toContainText("mapi plugin push --plugin acme --project growth --wait");
  await shot(page, info, "plugins-upload-empty");

  // a compiled wheel is refused before anything is sent
  await page.getByTestId("plugin-upload-drop").locator('input[type="file"]').setInputFiles(wheel("t_k3vq7m_acme-0.4.0-cp312-cp312-linux_x86_64.whl"));
  await expect(page.getByText("Only pure-Python wheels (py3-none-any) can be uploaded.")).toBeVisible();
  await expect(page.getByTestId("plugin-upload-start")).toBeDisabled();

  await page.getByTestId("plugin-upload-drop").locator('input[type="file"]').setInputFiles(wheel("t_k3vq7m_acme-0.4.0-py3-none-any.whl"));
  await page.getByTestId("plugin-upload-start").click();
  const result = page.getByTestId("plugin-upload-result");
  await expect(result).toBeVisible();
  await expect(page.getByTestId("plugin-check-steps")).toBeVisible();
  await expect(result.getByTestId("plugin-state-chip")).toHaveText(/Checking the files|Running the tests/);
  await expect(result).toHaveAttribute("data-state", "kit_passed", { timeout: 15_000 });
  await expect(result.getByTestId("plugin-state-chip")).toHaveText("Ready for you");
  const kit = result.getByTestId("plugin-kit-report");
  await expect(kit.getByRole("heading", { level: 4 })).toHaveText(["Passed (5)", "Skipped (1)"]);
  const sbom = result.getByTestId("plugin-sbom");
  await expect(sbom.getByTestId("plugin-sbom-files")).toHaveText("18");
  await expect(sbom).toContainText("2.1.3");
  await shot(page, info, "plugins-upload-done");
  // the bytes went to the storage bucket, never through the API
  expect(posts.some((p) => p.endsWith("/mock-plugin-upload"))).toBe(true);
  expect(posts.some((p) => /\/v1\/plugins\/[^/]+\/uploads\/[^/]+:complete$/.test(p))).toBe(true);

  await result.getByRole("link", { name: "Open the version page" }).click();
  await expect(page.getByTestId("plugin-version-header")).toContainText("0.4.0");
  await expect(page.getByTestId("plugin-only-you")).toHaveText("Only you can use this");
  await expect(page.getByTestId("plugin-request-approval")).toBeVisible();

  // in-app navigation: the mock keeps the new version in memory for this page load only
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Catalog" }).click();
  await page.getByRole("tab", { name: /Yours/ }).click();
  const section = page.getByTestId("uploaded-section");
  const card = section.getByTestId("uploaded-card").filter({ hasText: "Acme store tools" });
  await expect(card.getByTestId("plugin-state-chip")).toHaveText("Ready for you");
  await expect(card.getByTestId("plugin-only-you")).toHaveText("Only you can use this");
  await expect(card.getByTestId("uploaded-by-chip")).toHaveText("Uploaded by you");
  await shot(page, info, "plugins-yours-uploaded");
});

test("a refused file lists its problems in plain words", async ({ page }) => {
  await page.goto(`/plugins/${PLUGIN}/versions/${V.refused}`);
  const problems = page.getByTestId("plugin-problems");
  await expect(problems.getByRole("listitem")).toHaveCount(5);
  await expect(problems).toContainText("Compiled code");
  await expect(problems).toContainText("Requirement is not in the base image");
  await expect(page.getByTestId("plugin-state-chip")).toHaveText("Refused");
});

test("the reviewer panel: facts first, safe links, untrusted text as text, approve with the page's digests", async ({ page }, info) => {
  const decisions: unknown[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /:approve$/.test(r.url())) decisions.push(r.postDataJSON());
  });
  await page.goto(`/plugins/${PLUGIN}/versions/${V.awaiting}`);
  const panel = page.getByTestId("plugin-reviewer-panel");
  await expect(panel).toBeVisible();
  const tops = await Promise.all(
    ["plugin-inspect-facts", "plugin-sbom", "plugin-urls", "plugin-kit-report", "plugin-exports-diff"].map(async (id) => (await panel.getByTestId(id).boundingBox())?.y ?? -1),
  );
  expect([...tops].sort((a, b) => a - b)).toEqual(tops);

  const urls = panel.getByTestId("plugin-urls");
  await expect(urls.getByRole("link")).toHaveCount(2);
  await expect(urls.getByRole("link").first()).toHaveAttribute("rel", "noopener noreferrer nofollow ugc");
  await expect(urls).toContainText("xn--bcher-kva.example");
  await expect(urls).toContainText("javascript:alert(1)");
  expect(await page.locator('a[href^="javascript:"]').count()).toBe(0);
  expect(await page.locator("main img").count()).toBe(0);
  await expect(panel).toContainText("<img src=x onerror=alert(1)>Winsorize");
  await expect(panel).toContainText("Compared with 0.1.0, the last approved version");
  await shot(page, info, "plugins-review");

  await page.getByTestId("plugin-approve").click();
  await expect(page.getByTestId("plugin-version-page")).toHaveAttribute("data-state", "approved");
  await expect(page.getByTestId("plugin-state-chip")).toHaveText("Approved for the team");
  expect(decisions).toHaveLength(1);
  expect(decisions[0]).toEqual({
    overlay_sha256: expect.stringMatching(/^sha256:/),
    describe_digest: expect.stringMatching(/^sha256:/),
    base_image_digest: expect.stringMatching(/^sha256:/),
  });
});

test("the uploader cannot approve their own version and sees why", async ({ page }) => {
  await page.goto(`/plugins/${PLUGIN}/versions/${V.awaitingOwn}`);
  await expect(page.getByTestId("plugin-approve")).toBeDisabled();
  await expect(page.getByTestId("plugin-reject")).toBeDisabled();
  await expect(page.getByTestId("plugin-decision-blocked")).toHaveText("You uploaded this version, so another admin needs to review it.");
});

test("a stale sign-in goes through the identity provider and back, keeping the note", async ({ page }) => {
  await mock(page, { "mapi.mock.pluginReauth": "1" });
  const url = `/plugins/${PLUGIN}/versions/${V.awaiting}`;
  await page.goto(url);
  await page.getByLabel("Note").fill("Checked the links and the requirements.");
  await page.getByTestId("plugin-approve").click();
  await expect(page.getByText(/approving needs a sign-in from the last 15 minutes/)).toBeVisible();
  await page.getByTestId("plugin-reauth").click();
  await expect(page).toHaveURL(/\/auth\/login\?return_to=/);
  await page.getByRole("button", { name: /Sign in as Ana Analyst/ }).click();
  await expect(page).toHaveURL(new RegExp(`${url}$`));
  await expect(page.getByLabel("Note")).toHaveValue("Checked the links and the requirements.");
  await page.getByTestId("plugin-approve").click();
  await expect(page.getByTestId("plugin-version-page")).toHaveAttribute("data-state", "approved");
  await expect(page.getByTestId("plugin-decisions")).toContainText("Checked the links and the requirements.");
});

test("withdraw shows what uses the version before anything happens", async ({ page }) => {
  await page.goto(`/plugins/${PLUGIN}/versions/${V.approved}`);
  await page.getByTestId("plugin-yank").click();
  const usage = page.getByTestId("plugin-usage");
  await expect(usage.getByTestId("plugin-usage-runs")).toContainText("14");
  await expect(usage).toContainText("Endpoints");
  await page.getByLabel("Why").selectOption("superseded");
  await page.getByTestId("plugin-yank-confirm").click();
  await expect(page.getByTestId("plugin-version-page")).toHaveAttribute("data-state", "yanked");
  await expect(page.getByTestId("plugin-state-chip")).toHaveText("Withdrawn");
});

test("settings: a tenant admin turns private use off and confirms an uploader", async ({ page }) => {
  await page.goto("/settings");
  const card = page.getByTestId("plugin-settings-card");
  await expect(card).toContainText("k3vq7m");
  const toggle = card.getByTestId("plugin-private-use");
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  const uploaders = card.getByTestId("plugin-uploaders");
  await uploaders.getByRole("button", { name: /Revoke Rui Runner/ }).click();
  await expect(uploaders.getByRole("list", { name: "Confirmed uploaders" })).toHaveCount(0);
  await uploaders.getByLabel("Confirm an editor").selectOption({ label: "Rui Runner" });
  await uploaders.getByLabel(/I know who this person is/).check();
  await uploaders.getByRole("button", { name: "Confirm" }).click();
  await expect(uploaders.getByRole("list", { name: "Confirmed uploaders" })).toContainText("Rui Runner");
});

test("the dev banner says uploads run without a sandbox", async ({ page }) => {
  await mock(page, { "mapi.mock.devUntrusted": "1" });
  await page.goto(`/plugins/new?plugin=${PLUGIN}`);
  await expect(page.getByTestId("dev-untrusted-banner")).toContainText("Development only");
});

test("390 px: the upload, version and Yours pages never scroll sideways", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const url of [`/plugins/new?plugin=${PLUGIN}`, `/plugins/${PLUGIN}/versions/${V.awaiting}`, `/plugins/${PLUGIN}/versions/${V.kitFailed}`, `/plugins/${PLUGIN}`, "/catalog?tab=yours"]) {
    await page.goto(url);
    await expect(page.locator("main h1, main h2, main h3").first()).toBeVisible();
    await page.waitForTimeout(300);
    await noSideScroll(page);
  }
});
