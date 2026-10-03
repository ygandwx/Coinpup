import { expect, test } from "@playwright/test";

const username = process.env.E2E_USERNAME || "admin-e2e";
const password = process.env.E2E_PASSWORD;
if (!password) throw new Error("Set E2E_PASSWORD for the real test administrator before running E2E. Never target a production account.");

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
  });
});

test("an anonymous session reaches login once, and language persists", async ({ page }) => {
  let sessionRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/auth/session") sessionRequests += 1;
  });
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  // A bounded observation checks that an expected 401 isn't causing a retry loop.
  await page.waitForTimeout(400);
  expect(sessionRequests).toBe(1);
  await page.getByRole("button", { name: "中文", exact: true }).click();
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("an incorrect password is rejected by the real API", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill("intentionally-invalid-e2e-password");
  const responsePromise = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/auth/login" && response.request().method() === "POST");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  expect((await responsePromise).status()).toBe(401);
  await expect(page.getByRole("alert")).toContainText("The username or password is incorrect");
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  expect((await page.request.get("/api/v1/auth/session")).status()).toBe(401);
});

test("real login, refresh and CSRF-protected logout form a complete session", async ({ page, context }, testInfo) => {
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByTestId("current-username")).toHaveText(username);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByTestId("live-status")).toHaveText("Available");
  await expect(page.getByTestId("ready-status")).toHaveText("Available");
  const sessionResponse = await page.request.get("/api/v1/auth/session");
  expect(sessionResponse.status()).toBe(200);
  const session = await sessionResponse.json();
  expect(session.user.username).toBe(username);
  expect((await context.cookies()).some((cookie) => cookie.httpOnly)).toBe(true);
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain(password);
  expect(storage).not.toContain(session.csrf_token);
  await page.reload();
  await expect(page.getByTestId("current-username")).toHaveText(username);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByTestId("ready-status")).toHaveText("Available");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("workspace.png"), fullPage: true });
  const logoutResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/auth/logout" && response.request().method() === "POST");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  expect((await logoutResponse).status()).toBe(204);
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  expect((await page.request.get("/api/v1/auth/session")).status()).toBe(401);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
});

test("a failed initial connection can be retried without faking authentication", async ({ page }) => {
  // Transport failure only: the retry goes to the actual API and expects its anonymous 401.
  await page.route("**/api/v1/auth/session", (route) => route.abort("connectionfailed"));
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "We couldn't open your workspace" })).toBeVisible();
  await page.unroute("**/api/v1/auth/session");
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
});
