import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, login } from "./helpers";
import { seed } from "./ocr-helpers";
import { parseAmount } from "../src/money";
import type { Account, Category, OperationState } from "../src/ledger-api";

async function prepare(page: Page, checkUnready = false) {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed(false, undefined, 3),
        base = `/api/v1/ledgers/${fixture.ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: "Fictional batch bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const category = await api<Category>(page, "POST", `${base}/categories`, {
        name: "Fictional batch office",
        kind: "expense",
    });
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    if (checkUnready) {
        await page.getByTestId(`select-draft-${fixture.drafts[0]}`).check();
        await page.getByRole("button", { name: "Review selected drafts", exact: true }).click();
        await expect(page.getByText(/Selected drafts are not all ready/u)).toBeVisible();
        expect(await api(page, "GET", `${base}/operations`)).toEqual([]);
        await page.getByTestId(`select-draft-${fixture.drafts[0]}`).uncheck();
    }
    for (const [index, id] of fixture.drafts.entries()) {
        let release = () => {};
        const pattern = `**/ocr-drafts/${id}/review`;
        if (checkUnready && index === 1) {
            const gate = new Promise<void>((resolve) => {
                release = resolve;
            });
            await page.route(
                pattern,
                async (route) => {
                    await gate;
                    await route.continue();
                },
                { times: 1 },
            );
        }
        await page.getByTestId(`ocr-draft-${id}`).click();
        if (checkUnready && index === 1) {
            try {
                await expect(page.getByLabel("Amount · Reviewed", { exact: true })).toHaveCount(0);
            } finally {
                release();
            }
        }
        // A previously checked row must never authorize the newly selected row.
        await expect(page.getByTestId(`ocr-review-${id}`)).toBeVisible();
        for (const field of ["Amount", "Currency", "Date"])
            await page.getByLabel(`${field} · Reviewed`, { exact: true }).check();
        for (const field of ["Amount", "Currency", "Date"])
            await expect(page.getByLabel(`${field} · Reviewed`, { exact: true })).toBeChecked();
        await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
        await page.getByRole("button", { name: "Use reviewed candidates", exact: true }).click();
        const amount = await page.getByLabel("Amount", { exact: true }).inputValue();
        await page.getByLabel("Account", { exact: true }).selectOption(account.id);
        await page.getByLabel("Recognition date", { exact: true }).fill("2031-07-18");
        await page.getByLabel("Category", { exact: true }).selectOption(category.id);
        await page.getByLabel("Split amount", { exact: true }).fill(amount);
        await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
        await expect(
            page.getByRole("button", { name: "Edit prepared entry", exact: true }),
        ).toBeVisible();
    }
    return { ...fixture, base, drafts: fixture.drafts as string[] };
}
async function total(page: Page, base: string) {
    const rows = await api<OperationState[]>(page, "GET", `${base}/operations`);
    return rows.reduce((sum, row) => {
        if (row.latest_posting.kind !== "expense") throw new Error("Expected fictional expense");
        return sum + parseAmount(row.latest_posting.amount, 2);
    }, 0n);
}
async function select(page: Page, ids: string[]) {
    for (const id of ids) await page.getByTestId(`select-draft-${id}`).check();
    await page.getByRole("button", { name: "Review selected drafts", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Confirm selected drafts", exact: true }),
    ).toBeEnabled();
}
test("only selected reviewed rows post: first 30 USD, then 60 USD", async ({ page }, info) => {
    const data = await prepare(page, true);
    expect(await total(page, data.base)).toBe(0n);
    await select(page, data.drafts.slice(0, 2));
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-batch-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    await page.getByRole("button", { name: "Confirm selected drafts", exact: true }).click();
    await expect.poll(() => total(page, data.base)).toBe(3000n);
    await expect(page.getByTestId(`select-draft-${data.drafts[0]}`)).toBeDisabled();
    await select(page, data.drafts.slice(2));
    await page.getByRole("button", { name: "Confirm selected drafts", exact: true }).click();
    await expect.poll(() => total(page, data.base)).toBe(6000n);
    expect(await api<OperationState[]>(page, "GET", `${data.base}/operations`)).toHaveLength(3);
});
test("unknown middle row pauses later rows and retries its original intent", async ({ page }) => {
    const data = await prepare(page),
        requests: string[] = [];
    await select(page, data.drafts);
    await page.route("**/ocr-drafts/*/confirmations", async (route) => {
        requests.push(route.request().postData()!);
        if (requests.length === 2) {
            const result = await route.fetch();
            expect(result.status()).toBe(201);
            await route.abort("failed");
        } else await route.continue();
    });
    await page.getByRole("button", { name: "Confirm selected drafts", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original confirmation", exact: true }),
    ).toBeVisible();
    expect(requests).toHaveLength(2);
    expect(await total(page, data.base)).toBe(3000n);
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect.poll(() => total(page, data.base)).toBe(6000n);
    expect(requests).toHaveLength(4);
    expect(requests[1]).toBe(requests[2]);
    expect(await api<OperationState[]>(page, "GET", `${data.base}/operations`)).toHaveLength(3);
});
