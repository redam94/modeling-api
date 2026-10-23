/**
 * v0.20 "Connect a chat tool" (MCP read tokens) against the MSW-mocked API (`vite --mode mock`,
 * production build with the CSP and Trusted Types enforced):
 *
 * - the analyst makes a read token in the builder's preview: the secret shows once, the
 *   configuration snippets (stdio bridge and HTTP) hold it, it is never stored in the browser,
 *   and it is gone when the dialog closes; editors see everyone's tokens and revoke their own;
 * - the limit of active tokens per person and dashboard is said in plain words;
 * - a client user makes and revokes their own token in the portal (no "everyone's tokens");
 * - a share link offers no "Connect a chat tool";
 * - the keyboard, a phone width and an accessibility check of the dialog in both themes.
 *
 * The seeded tokens of the mock (`src/mocks/chat.ts`) are on the layout 2 forecast dashboard of
 * acme/retail: Ana's "Claude Desktop" and Cleo's "Cleo's laptop". The mock keeps its state in
 * memory per page load, so each test starts from them.
 *
 * Every test fails on a page error, a CSP violation (Trusted Types included), an unexpected
 * console error or an unexpected failed API call. Screenshots land in the test output folder
 * (and in MAPI_SHOTS_DIR when set).
 */
import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

/** The layout 2 forecast dashboard of acme/retail (`V2_DASHBOARD_IDS.forecast`). */
const FORECAST = "dsh_01jaf3w2fcst2visits3x4y5z6";
/** Its share link (`MOCK_SHARE_TOKENS.forecastV2`): no passcode. */
const FORECAST_LINK = `mpd_${"f2v3w4x5".repeat(5)}`;
const SECRET = /^mapi_[0-9a-z]{8}_[0-9a-z]{39}$/;
const SHOTS = process.env.MAPI_SHOTS_DIR;

type Persona = "ana" | "cleo" | "signed-out";

/** Sign in as a persona in acme/retail, once per test (chat on, so the portal shows its launcher too). */
async function as(page: Page, persona: Persona) {
  await page.addInitScript((p) => {
    if (window.top !== window || sessionStorage.getItem("mapi.test.persona")) return;
    localStorage.setItem("mapi.mock.session", p);
    if (p !== "signed-out") localStorage.setItem("mapi.scope", JSON.stringify({ state: { preferred: { tenantId: "acme", projectId: "retail" }, recent: [] }, version: 1 }));
    localStorage.setItem("mapi.mock.chatOn", "1");
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
  const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(scroll, `the page is wider than ${client} px`).toBeLessThanOrEqual(client);
}

/** Everything the browser keeps for this origin (storage and cookies), as one string. */
async function stored(page: Page): Promise<string> {
  return page.evaluate(() => [localStorage, sessionStorage].flatMap((s) => Object.keys(s).map((k) => `${k}=${s.getItem(k) ?? ""}`)).join("\n") + document.cookie);
}

/** The read-token calls the page sends. */
function trackTokens(page: Page): { method: string; path: string; body: unknown }[] {
  const seen: { method: string; path: string; body: unknown }[] = [];
  page.on("request", (r) => {
    const path = new URL(r.url()).pathname;
    if (!/\/read-tokens(\/|$)/.test(path)) return;
    let body: unknown = null;
    try {
      body = r.postDataJSON();
    } catch {
      body = r.postData();
    }
    seen.push({ method: r.method(), path, body });
  });
  return seen;
}

// ---------------------------------------------------------------------------------- the audit

/**
 * A small accessibility audit of `root` (the same rules as in chat.spec.ts; the web app has no
 * axe dependency and v0.20 adds none): every control has a name, label and description
 * references resolve, ids are unique, images have alt text, lists hold list items, no control
 * nests in another, a dialog has a name, and text meets the WCAG AA contrast ratio over its
 * real background (4.5:1, 3:1 for large text). Returns the problems found.
 */
async function a11yAudit(root: Locator): Promise<string[]> {
  return root.evaluate((rootEl: Element) => {
    const problems: string[] = [];
    const doc = rootEl.ownerDocument;
    const visible = (el: Element) => {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return r.width > 1 && r.height > 1 && cs.visibility !== "hidden" && cs.display !== "none";
    };
    const text = (el: Element | null) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
    const byIds = (ids: string) => ids.split(/\s+/).filter(Boolean).map((id) => doc.getElementById(id));
    const nameOf = (el: Element): string => {
      const label = el.getAttribute("aria-label")?.trim();
      if (label) return label;
      const by = el.getAttribute("aria-labelledby");
      if (by) return byIds(by).map(text).join(" ").trim();
      const labels = (el as HTMLInputElement).labels;
      if (labels?.length) return [...labels].map(text).join(" ").trim();
      const own = text(el) || [...el.querySelectorAll("img[alt]")].map((i) => i.getAttribute("alt") ?? "").join(" ").trim();
      if (own && !["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return own;
      return (el.getAttribute("title") ?? (el as HTMLInputElement).placeholder ?? "").trim();
    };

    const controls = rootEl.querySelectorAll(
      "button, a[href], input:not([type=hidden]), textarea, select, [role=button], [role=switch], [role=checkbox], [role=tab], [role=radio], [role=combobox], [role=slider], [role=menuitem]",
    );
    for (const el of controls) {
      if (!visible(el) && el.tagName !== "INPUT") continue;
      if (!nameOf(el)) problems.push(`no accessible name: <${el.tagName.toLowerCase()} class="${String(el.getAttribute("class")).slice(0, 60)}">`);
    }
    for (const el of [rootEl, ...rootEl.querySelectorAll("[aria-labelledby], [aria-describedby]")]) {
      for (const attr of ["aria-labelledby", "aria-describedby"]) {
        const v = el.getAttribute(attr);
        if (v && byIds(v).some((x) => x === null)) problems.push(`${attr}="${v}" names a missing id`);
      }
    }
    const ids = [...rootEl.querySelectorAll("[id]")].map((e) => e.id);
    for (const id of new Set(ids)) if (doc.querySelectorAll(`[id="${CSS.escape(id)}"]`).length > 1) problems.push(`duplicate id "${id}"`);
    for (const img of rootEl.querySelectorAll("img")) if (!img.hasAttribute("alt")) problems.push(`image without alt: ${img.getAttribute("src")}`);
    for (const list of rootEl.querySelectorAll("ul, ol")) {
      for (const child of list.children) if (!["LI", "SCRIPT", "TEMPLATE"].includes(child.tagName)) problems.push(`<${list.tagName.toLowerCase()}> holds <${child.tagName.toLowerCase()}>`);
    }
    for (const el of rootEl.querySelectorAll("button button, button a[href], a[href] button, a[href] a[href]")) problems.push(`nested control: ${text(el).slice(0, 40)}`);
    for (const d of [rootEl, ...rootEl.querySelectorAll("[role=dialog], [role=alertdialog]")]) {
      if (/^(dialog|alertdialog)$/.test(d.getAttribute("role") ?? "") && !nameOf(d)) problems.push("a dialog without a name");
    }

    const canvas = doc.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true })!;
    type RGBA = [number, number, number, number];
    const rgba = (c: string): RGBA => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = "#000";
      ctx.fillStyle = c;
      ctx.fillRect(0, 0, 1, 1);
      const d = ctx.getImageData(0, 0, 1, 1).data;
      return [d[0]!, d[1]!, d[2]!, d[3]! / 255];
    };
    const over = (top: RGBA, below: RGBA): RGBA => [0, 1, 2].map((i) => top[i]! * top[3] + below[i]! * (1 - top[3])).concat(1) as RGBA;
    const lum = ([r, g, b]: RGBA) => {
      const f = (v: number) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const background = (el: Element): RGBA | null => {
      const layers: RGBA[] = [];
      for (let e: Element | null = el; e; e = e.parentElement) {
        const cs = getComputedStyle(e);
        if (cs.backgroundImage !== "none" || Number(cs.opacity) < 1) return null;
        const c = rgba(cs.backgroundColor);
        if (c[3] > 0) layers.push(c);
        if (c[3] >= 1) break;
      }
      let out: RGBA = [255, 255, 255, 1];
      for (const layer of layers.reverse()) out = over(layer, out);
      return out;
    };
    const walker = doc.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT);
    const checked = new Set<Element>();
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const el = n.parentElement;
      if (!el || checked.has(el) || !(n.textContent ?? "").trim() || !visible(el)) continue;
      checked.add(el);
      if (el.closest("[disabled], [aria-disabled=true], fieldset:disabled, .sr-only, [aria-hidden=true]")) continue;
      const bg = background(el);
      if (!bg) continue;
      const cs = getComputedStyle(el);
      const fg = over(rgba(cs.color), bg);
      const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x) as [number, number];
      const ratio = (a + 0.05) / (b + 0.05);
      const size = Number.parseFloat(cs.fontSize);
      const large = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 700);
      if (ratio < (large ? 3 : 4.5) - 0.01) problems.push(`contrast ${ratio.toFixed(2)}:1 for "${text(el).slice(0, 50)}"`);
    }
    return problems;
  });
}

// ---------------------------------------------------------------------------------- checks

const allowedFailures = new WeakMap<Page, RegExp[]>();
const consoleErrors = new WeakMap<Page, string[]>();
const failedCalls = new WeakMap<Page, string[]>();

function expectFailures(page: Page, ...calls: RegExp[]) {
  allowedFailures.set(page, [...(allowedFailures.get(page) ?? []), ...calls]);
}

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
  const errors: string[] = [];
  const failed: string[] = [];
  consoleErrors.set(page, errors);
  failedCalls.set(page, failed);
  page.on("console", (m) => {
    if (m.type() === "error" && !/^Failed to load resource: the server responded with a status of \d+ /.test(m.text())) errors.push(m.text());
  });
  page.on("response", (r) => {
    if (r.status() < 400) return;
    const call = `${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`;
    if (!(allowedFailures.get(page) ?? []).some((re) => re.test(call))) failed.push(call);
  });
  await page.addInitScript(() => {
    const w = window as unknown as { __csp: string[] };
    w.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
});

test.afterEach(async ({ page }) => {
  const violations = await page.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []);
  expect(violations, "CSP or Trusted Types violations").toEqual([]);
  expect(consoleErrors.get(page) ?? [], "console errors").toEqual([]);
  expect(failedCalls.get(page) ?? [], "failed API calls").toEqual([]);
});

// ---------------------------------------------------------------------------------- helpers

/** Open "Connect a chat tool" from the builder's preview of the forecast dashboard, as the analyst. */
async function openFromPreview(page: Page): Promise<Locator> {
  await page.goto(`/dashboards/${FORECAST}?tab=preview`);
  await expect(page.getByRole("tabpanel", { name: "Preview" }).getByRole("heading", { level: 1, name: "Monthly store visits" })).toBeVisible();
  await page.getByTestId("connect-chat-tool").click();
  return dialogOf(page);
}

async function dialogOf(page: Page): Promise<Locator> {
  const dialog = page.getByTestId("connect-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "Connect a chat tool" })).toBeVisible();
  return dialog;
}

/** Make a token; returns its secret (read from the shown-once field). */
async function makeToken(dialog: Locator, name: string, days?: number): Promise<string> {
  const form = dialog.getByTestId("read-token-create");
  await form.getByLabel("Name").fill(name);
  if (days) await form.getByLabel("Ends after").selectOption(String(days));
  await form.getByRole("button", { name: "Make token" }).click();
  const shown = dialog.getByTestId("read-token-secret");
  await expect(shown).toBeVisible();
  const secret = await shown.getByLabel(`Read token “${name}”`).inputValue();
  expect(secret).toMatch(SECRET);
  return secret;
}

function rows(dialog: Locator): Locator {
  return dialog.getByTestId("read-token-row");
}

// ---------------------------------------------------------------------------------- the analyst

test.describe("the analyst", () => {
  test("makes a read token: the secret shows once, the snippets hold it, the browser keeps nothing", async ({ page }, info) => {
    const seen = trackTokens(page);
    await as(page, "ana");
    const dialog = await openFromPreview(page);
    await expect(dialog).toContainText("Connect only tools that you trust. If the same tool can also send e-mail or open web pages, it can send the dashboard's numbers to other people.");

    // Before a token is made the snippets hold a placeholder, never a secret.
    const stdio = dialog.getByTestId("connect-snippet-stdio");
    await expect(stdio).toContainText('"command": "mapi-mcp"');
    await expect(stdio).toContainText('"MAPI_DASHBOARD_TOKEN": "<your read token>"');
    await expect(stdio).toContainText('"monthly-store-visits"');
    // Ana's own seeded token.
    await expect(rows(dialog)).toHaveCount(1);
    await expect(rows(dialog).first()).toContainText("Claude Desktop");
    await expect(rows(dialog).first()).toHaveAttribute("data-status", "active");

    // A name is necessary.
    await dialog.getByTestId("read-token-create").getByRole("button", { name: "Make token" }).click();
    expect(seen.filter((r) => r.method === "POST")).toEqual([]);

    const secret = await makeToken(dialog, "Laptop assistant", 60);
    const post = seen.find((r) => r.method === "POST")!;
    expect(post.path).toBe(`/v1/dashboards/${FORECAST}/read-tokens`);
    expect(post.body).toEqual({ name: "Laptop assistant", expires_in_days: 60 });
    await expect(dialog.getByTestId("read-token-secret")).toContainText("Copy the token now. The app shows it only one time.");

    // The snippets now hold the new secret: in the environment (stdio) or the header (HTTP), never in a URL.
    await expect(stdio).toContainText(`"MAPI_DASHBOARD_TOKEN": "${secret}"`);
    await expect(stdio).toContainText('"MAPI_API_URL": "');
    await dialog.getByText("Web or remote (HTTP)").click();
    const http = dialog.getByTestId("connect-snippet-http");
    await expect(http).toContainText(`"Authorization": "Bearer ${secret}"`);
    await expect(http).toContainText(/"url": "[^"]+\/v1\/mcp"/);
    expect(await http.innerText()).not.toMatch(/[?&](token|key)=/);

    // The new token is in the list; the secret is not (only its prefix).
    await expect(rows(dialog)).toHaveCount(2);
    const fresh = rows(dialog).filter({ hasText: "Laptop assistant" });
    await expect(fresh).toHaveAttribute("data-status", "active");
    await expect(fresh).toContainText(`${secret.slice(5, 13)}…`);
    await expect(dialog.getByRole("table", { name: "Read tokens" })).not.toContainText(secret);
    // Never in the browser's stores.
    expect(await stored(page)).not.toContain(secret);
    await shot(page, info, "connect-dialog-new-token");

    // Closing without a copy asks first; then the secret is gone for good.
    page.once("dialog", (d) => {
      expect(d.message()).toBe("Close the window? You did not copy the token, and the app cannot show it again.");
      void d.accept();
    });
    await dialog.getByRole("button", { name: "Close", exact: true }).last().click();
    await expect(dialog).toHaveCount(0);
    expect(await stored(page)).not.toContain(secret);
    expect(await page.content()).not.toContain(secret);
    await page.getByTestId("connect-chat-tool").click();
    const again = await dialogOf(page);
    await expect(again.getByTestId("read-token-secret")).toHaveCount(0);
    await expect(again.getByTestId("connect-snippet-stdio")).toContainText("<your read token>");
    await expect(rows(again)).toHaveCount(2);
  });

  test("sees everyone's tokens and revokes a token", async ({ page }) => {
    const seen = trackTokens(page);
    await as(page, "ana");
    const dialog = await openFromPreview(page);
    await expect(dialog.getByRole("heading", { name: "Your read tokens" })).toBeVisible();
    await dialog.getByLabel("Show everyone's tokens").check();
    await expect(dialog.getByRole("heading", { name: "Read tokens of this dashboard" })).toBeVisible();
    await expect(rows(dialog)).toHaveCount(2);
    const cleos = rows(dialog).filter({ hasText: "Cleo's laptop" });
    await expect(cleos).toContainText("Cleo Client");
    expect(seen.some((r) => r.method === "GET" && r.path.endsWith("/read-tokens"))).toBe(true);

    // Revoke asks once, then the token is revoked at once.
    const mine = rows(dialog).filter({ hasText: "Claude Desktop" });
    await mine.getByRole("button", { name: "Revoke Claude Desktop" }).click();
    await mine.getByRole("button", { name: "Keep" }).click();
    await expect(mine).toHaveAttribute("data-status", "active");
    await mine.getByRole("button", { name: "Revoke Claude Desktop" }).click();
    await mine.getByRole("button", { name: "Revoke now" }).click();
    await expect(page.getByText("Token revoked")).toBeVisible();
    await expect(mine).toHaveAttribute("data-status", "revoked");
    await expect(mine.getByRole("button", { name: /^Revoke/ })).toHaveCount(0);
    const del = seen.find((r) => r.method === "DELETE");
    expect(del?.path).toBe(`/v1/dashboards/${FORECAST}/read-tokens/tok_01jaf3r1s2t3v4w5x6y7z8chat`);
  });

  test("the limit of active tokens is said in plain words", async ({ page }) => {
    expectFailures(page, new RegExp(`^409 POST /v1/dashboards/${FORECAST}/read-tokens$`));
    await as(page, "ana");
    const dialog = await openFromPreview(page);
    // One seeded token, four more: five active.
    for (const n of [1, 2, 3, 4]) {
      await makeToken(dialog, `Tool ${n}`);
      await dialog.getByRole("button", { name: "Make another token" }).click();
    }
    await expect(rows(dialog).and(dialog.locator("[data-status=active]"))).toHaveCount(5);
    const form = dialog.getByTestId("read-token-create");
    await form.getByLabel("Name").fill("Tool 5");
    await form.getByRole("button", { name: "Make token" }).click();
    await expect(form.getByText("You have 5 active tokens for this dashboard. Revoke one before you make a new one.")).toBeVisible();
    await expect(dialog.getByTestId("read-token-secret")).toHaveCount(0);
  });

  test("works with the keyboard only", async ({ page }) => {
    await as(page, "ana");
    await page.goto(`/dashboards/${FORECAST}?tab=preview`);
    const button = page.getByTestId("connect-chat-tool");
    await expect(button).toBeVisible();
    await button.focus();
    await page.keyboard.press("Enter");
    const dialog = await dialogOf(page);
    await dialog.getByLabel("Name").focus();
    await page.keyboard.type("Keyboard tool");
    await page.keyboard.press("Enter");
    await expect(dialog.getByTestId("read-token-secret")).toBeVisible();
    // The copy button is reachable; a copy marks the secret as kept, so Escape closes without asking.
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await dialog.getByRole("button", { name: "Copy read token" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByText("Copied to the clipboard").first()).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(button).toBeFocused();
  });
});

// ---------------------------------------------------------------------------------- a client user

test.describe("a client user", () => {
  test("makes and revokes their own token in the portal", async ({ page }, info) => {
    const seen = trackTokens(page);
    await as(page, "cleo");
    await page.goto(`/c/${FORECAST}`);
    await expect(page.getByRole("heading", { level: 1, name: "Monthly store visits" })).toBeVisible();
    await page.getByTestId("connect-chat-tool").click();
    const dialog = await dialogOf(page);
    // Only their own tokens, and no "everyone's tokens" for a client.
    await expect(dialog.getByLabel("Show everyone's tokens")).toHaveCount(0);
    await expect(rows(dialog)).toHaveCount(1);
    await expect(rows(dialog).first()).toContainText("Cleo's laptop");
    expect(seen.filter((r) => r.method === "GET").every((r) => r.path === `/v1/dashboards/${FORECAST}/read-tokens`)).toBe(true);

    const secret = await makeToken(dialog, "Cleo's assistant");
    await expect(dialog.getByTestId("connect-snippet-stdio")).toContainText(secret);
    await expect(rows(dialog)).toHaveCount(2);
    await shot(page, info, "connect-dialog-client");

    const row = rows(dialog).filter({ hasText: "Cleo's assistant" });
    await row.getByRole("button", { name: "Revoke Cleo's assistant" }).click();
    await row.getByRole("button", { name: "Revoke now" }).click();
    await expect(row).toHaveAttribute("data-status", "revoked");
    expect(seen.filter((r) => r.method === "DELETE")).toHaveLength(1);
    expect(await stored(page)).not.toContain(secret);
  });
});

// ---------------------------------------------------------------------------------- a share link

test("a share link offers no Connect a chat tool", async ({ page }) => {
  await as(page, "signed-out");
  await page.goto(`/s#${FORECAST_LINK}`);
  await expect(page.getByRole("heading", { level: 1, name: "Monthly store visits" })).toBeVisible();
  await expect(page.getByTestId("connect-chat-tool")).toHaveCount(0);
});

// ---------------------------------------------------------------------------------- phone width and themes

test.describe("at 390 px", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the dialog fits the phone width, with a new token", async ({ page }, info) => {
    await as(page, "ana");
    const dialog = await openFromPreview(page);
    await makeToken(dialog, "Phone check");
    const box = (await dialog.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
    await noHorizontalScroll(page);
    // The token table and the snippet scroll inside the dialog, never the page.
    const overflow = await dialog.evaluate((el) => {
      const right = el.getBoundingClientRect().right;
      return [...el.querySelectorAll<HTMLElement>("*")].filter((c) => {
        if (c.getBoundingClientRect().right <= right + 1) return false;
        // Inside a scroller of its own is fine.
        for (let p = c.parentElement; p && p !== el; p = p.parentElement) if (/(auto|scroll)/.test(getComputedStyle(p).overflowX)) return false;
        return true;
      }).length;
    });
    expect(overflow).toBe(0);
    await shot(page, info, "connect-dialog-390");
  });
});

for (const scheme of ["light", "dark"] as const) {
  test.describe(`the ${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    test("the dialog passes the accessibility audit, before and after a token is made", async ({ page }, info) => {
      await as(page, "ana");
      const dialog = await openFromPreview(page);
      await expect(page.locator("html")).toHaveClass(scheme === "dark" ? /\bdark\b/ : /^(?!.*\bdark\b)/);
      expect(await a11yAudit(dialog), "the dialog").toEqual([]);
      await makeToken(dialog, "Audit tool");
      await dialog.getByLabel("Show everyone's tokens").check();
      await expect(rows(dialog)).toHaveCount(3);
      expect(await a11yAudit(dialog), "the dialog with a new token and everyone's tokens").toEqual([]);
      await shot(page, info, `connect-dialog-${scheme}`);
    });
  });
}
