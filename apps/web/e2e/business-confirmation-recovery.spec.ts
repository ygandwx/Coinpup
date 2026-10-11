import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessConfirmationDetail } from "../src/business-confirmation-api";

test("fictional confirmation replays exact intent after lost success and real 401", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await confirmationSetup(page);
    await page.setViewportSize({ width: 320, height: 1000 });
    await page.getByRole("button", { name: "Review saved version", exact: true }).click();
    await page.getByRole("checkbox", { name: "I reviewed amounts", exact: false }).check();
    const bodies: string[] = [],
        keys: string[] = [];
    await page.route(`**${data.base}/confirm`, async (route) => {
        bodies.push(route.request().postData()!);
        keys.push(route.request().headers()["idempotency-key"]);
        if (bodies.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(201);
            return route.abort("failed");
        }
        if (bodies.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            const response = await page.request.post("/api/v1/auth/logout", {
                headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": auth.csrf_token },
            });
            expect(response.status()).toBe(204);
        }
        await route.continue();
    });
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original confirmation", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Back to documents", exact: true }),
    ).toBeDisabled();
    await expect(page.getByRole("button", { name: "Transactions", exact: true })).toBeDisabled();
    await expectNoOverflow(page);
    await page.screenshot({
        path: info.outputPath("confirmation-unknown-en-320.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect(page.locator(".business-confirmation-panel")).toHaveCount(0);
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original confirmation", exact: true }),
    ).toBeVisible();
    expect(bodies).toHaveLength(2);
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect(
        page.getByText("Confirmed; original amounts and journal mappings are sealed.", {
            exact: true,
        }),
    ).toBeVisible();
    expect(bodies).toHaveLength(3);
    expect(new Set(bodies).size).toBe(1);
    expect(new Set(keys).size).toBe(1);
    const detail = await api<BusinessConfirmationDetail>(page, "GET", `${data.base}/confirmation`);
    expect(detail.document.version).toBe(2);
    expect(detail.confirmation.postings).toHaveLength(1);
    expect(detail.confirmation.id).toBe(JSON.parse(bodies[0]).id);
    expect(await api(page, "GET", `${data.ledger}/business-documents`)).toEqual([]);
});
