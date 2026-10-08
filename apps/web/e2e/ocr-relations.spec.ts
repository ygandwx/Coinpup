import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, login } from "./helpers";
import { seed } from "./ocr-helpers";
import type { Account, Category, OperationResponse, OperationState } from "../src/ledger-api";

async function existing(page: Page, ledger: string) {
    const base = `/api/v1/ledgers/${ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: `Fictional duplicate bank ${randomUUID()}`,
        kind: "bank",
        asset_ids: ["USD"],
    });
    const category = await api<Category>(page, "POST", `${base}/categories`, {
        name: `Fictional duplicate office ${randomUUID()}`,
        kind: "expense",
    });
    const operation = await api<OperationResponse>(page, "POST", `${base}/expenses`, {
        account_id: account.id,
        asset_id: "USD",
        amount: "10.00",
        transaction_date: "2031-07-18",
        recognition_date: "2031-07-18",
        splits: [{ category_id: category.id, amount: "10.00" }],
        description: "Fictional existing expense",
    });
    return { base, account, category, operation };
}
async function open(page: Page, entity: string) {
    await page.goto(`/?entity=${entity}&view=ocr`);
    await page.getByRole("button", { name: /^Draft 1 · Review needed$/u }).click();
    for (const field of ["Total", "Currency", "Document date"])
        await page.getByLabel(`${field} · Reviewed`, { exact: true }).check();
}
async function link(page: Page, id: string) {
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("link");
    await page.getByRole("button", { name: "Prepare entry", exact: true }).click();
    await page.getByLabel("Existing entry", { exact: true }).selectOption(id);
    await page.getByRole("button", { name: "Save link draft", exact: true }).click();
    await expect(page.getByText(/Saved link:/u)).toBeVisible();
}

test("linking does not repost, and identical source requires explicit acknowledgment", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const identity = randomUUID(),
        first = seed(false, identity);
    const original = await existing(page, first.ledger);
    await open(page, first.entity);
    await link(page, original.operation.id);
    await page.getByRole("button", { name: "Confirm link", exact: true }).click();
    await expect(page.getByText(/Document confirmation completed/u)).toBeVisible();
    expect(await api<OperationState[]>(page, "GET", `${original.base}/operations`)).toHaveLength(1);
    const repeated = seed(false, identity),
        target = await existing(page, repeated.ledger);
    await open(page, repeated.entity);
    await link(page, target.operation.id);
    const confirm = page.getByRole("button", { name: "Confirm link", exact: true });
    await expect(confirm).toBeDisabled();
    await page.getByRole("button", { name: /Inspect prior confirmation/u }).click();
    await expect(page.getByText(/Historical amount:/u)).toContainText("10.00 USD");
    const ack = page.getByLabel(
        "I reviewed the prior record and explicitly confirm this additional action",
        { exact: true },
    );
    await ack.check();
    await expect(confirm).toBeEnabled();
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-duplicate-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    await expect(ack).toBeChecked();
    await page.getByRole("button", { name: "Refresh duplicate hints", exact: true }).click();
    await expect(ack).not.toBeChecked();
    await expect(confirm).toBeDisabled();
    await ack.check();
    await confirm.click();
    await expect(page.getByText(/Document confirmation completed/u)).toBeVisible();
    expect(await api<OperationState[]>(page, "GET", `${target.base}/operations`)).toHaveLength(1);
});

test("cancelled selection is rejected; a different source with the same amount is only a hint", async ({
    page,
}) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed(),
        data = await existing(page, fixture.ledger);
    await open(page, fixture.entity);
    await link(page, data.operation.id);
    await api(page, "POST", `${data.base}/operations/${data.operation.id}/cancellations`, {
        expected_version: 1,
        reason: "Fictional concurrent cancellation",
    });
    const submitted = page.waitForResponse((response) => response.url().endsWith("/confirmations"));
    await page.getByRole("button", { name: "Confirm link", exact: true }).click();
    expect((await submitted).status()).toBe(409);
    await expect(page.getByText(/Request rejected/u)).toBeVisible();
    const review = await api<{ status: string }>(
        page,
        "GET",
        `${data.base}/ocr-drafts/${fixture.draft}/review`,
    );
    expect(review.status).toBe("draft");
    await page.getByRole("button", { name: "Dismiss confirmation status", exact: true }).click();
    const active = await existing(page, fixture.ledger);
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
    await page.getByRole("button", { name: "Use reviewed candidates", exact: true }).click();
    // The original account and category remain valid after cancelling their expense.
    await page.getByLabel("Account", { exact: true }).selectOption(data.account.id);
    await page.getByLabel("Recognition date", { exact: true }).fill("2031-07-18");
    await page.getByLabel("Category", { exact: true }).selectOption(data.category.id);
    await page.getByLabel("Split amount", { exact: true }).fill("10.00");
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    await expect(page.getByText(/Same-date, same-amount entries exist/u)).toBeVisible();
    await expect(
        page.getByLabel(
            "I reviewed the prior record and explicitly confirm this additional action",
            { exact: true },
        ),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    await expect(page.getByText(/Document confirmation completed/u)).toBeVisible();
    const operations = await api<OperationState[]>(
        page,
        "GET",
        `${data.base}/operations?status=active`,
    );
    expect(operations).toHaveLength(2);
    expect(operations.some((row) => row.id === active.operation.id)).toBe(true);
});
