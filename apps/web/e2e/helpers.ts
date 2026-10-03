import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";

export const username = process.env.E2E_USERNAME || "admin-e2e";

export function fictionalName(label: string): string {
  return `Fictional E2E ${label} ${randomUUID().slice(0, 8)}`;
}

export async function login(page: Page): Promise<void> {
  const password = process.env.E2E_PASSWORD;
  if (!password) throw new Error("Set E2E_PASSWORD for a disposable real administrator, never production.");
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByTestId("current-username")).toHaveText(username);
}

export async function api<T>(page: Page, method: "GET" | "POST" | "PATCH", path: string, data?: unknown): Promise<T> {
  const sessionResponse = await page.request.get("/api/v1/auth/session");
  expect(sessionResponse.status()).toBe(200);
  const session = await sessionResponse.json();
  const response = await page.request.fetch(path, {
    method,
    data,
    headers: {
      Origin: new URL(page.url()).origin,
      "X-CSRF-Token": session.csrf_token,
      "Idempotency-Key": randomUUID(),
    },
  });
  expect(response.ok(), `${method} ${path}: ${response.status()} ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

export async function selectLedger(page: Page, name: string): Promise<void> {
  await page.getByLabel("Ledger", { exact: true }).selectOption({ label: name });
}

export async function expectNoOverflow(page: Page): Promise<void> {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}
