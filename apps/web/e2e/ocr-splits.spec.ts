import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, login } from "./helpers";
import { seed } from "./ocr-helpers";
import type { Account, Balance, Category, HistoryEntry, OperationState } from "../src/ledger-api";
import type { ReviewView } from "../src/ocr-api";
import { parseAmount } from "../src/money";

test("a reviewed fictional receipt supports one category or exact splits without duplicate cash", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed(),
        base = `/api/v1/ledgers/${fixture.ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: "Fictional split bank",
        kind: "bank",
        asset_ids: ["CNY"],
    });
    const categories: Category[] = [];
    for (const name of ["Fictional office", "Fictional travel"])
        categories.push(
            await api<Category>(page, "POST", `${base}/categories`, { name, kind: "expense" }),
        );
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await page.getByTestId(`ocr-draft-${fixture.draft}`).click();
    for (const field of ["Total", "Currency", "Document date"])
        await page.getByLabel(`${field} · Reviewed`, { exact: true }).check();
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
    await page.getByRole("button", { name: "Use reviewed candidates", exact: true }).click();
    await page.getByLabel("Account", { exact: true }).selectOption(account.id);
    await page.getByLabel("Amount", { exact: true }).fill("120.00");
    await page.getByLabel("Recognition date", { exact: true }).fill("2031-07-18");
    const splits = page.getByTestId("split-row");
    await splits.nth(0).getByLabel("Category", { exact: true }).selectOption(categories[0].id);
    await splits.nth(0).getByLabel("Split amount", { exact: true }).fill("80.00");
    await page.getByRole("button", { name: "Add split", exact: true }).click();
    await splits.nth(1).getByLabel("Category", { exact: true }).selectOption(categories[1].id);
    await splits.nth(1).getByLabel("Split amount", { exact: true }).fill("50.00");
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("Split total minus principal: 10.00 CNY");
    expect(await api(page, "GET", `${base}/operations`)).toEqual([]);
    expect(
        (await api<ReviewView>(page, "GET", `${base}/ocr-drafts/${fixture.draft}/review`)).review
            .entry,
    ).toEqual({});
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(page.getByRole("alert")).toContainText(
                `${language === "zh" ? "拆分合计减本金" : "Split total minus principal"}: 10.00 CNY`,
            );
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-split-error-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    await page.getByRole("button", { name: "Remove split 2", exact: true }).click();
    await splits.nth(0).getByLabel("Split amount", { exact: true }).fill("120.00");
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Edit prepared entry", exact: true }),
    ).toBeVisible();
    const single = await api<ReviewView>(page, "GET", `${base}/ocr-drafts/${fixture.draft}/review`);
    expect(single.review.entry).toMatchObject({
        kind: "expense",
        command: { splits: [{ category_id: categories[0].id, amount: "120.00" }] },
    });
    expect(await api(page, "GET", `${base}/operations`)).toEqual([]);
    await page.getByRole("button", { name: "Edit prepared entry", exact: true }).click();
    await splits.nth(0).getByLabel("Split amount", { exact: true }).fill("80.00");
    await page.getByRole("button", { name: "Add split", exact: true }).click();
    await splits.nth(1).getByLabel("Category", { exact: true }).selectOption(categories[1].id);
    await splits.nth(1).getByLabel("Split amount", { exact: true }).fill("40.00");
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    await expect(page.getByRole("button", { name: "Confirm and post", exact: true })).toBeEnabled();
    const response = page.waitForResponse((r) =>
        r.url().endsWith(`/ocr-drafts/${fixture.draft}/confirmations`),
    );
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    expect((await response).status()).toBe(201);
    const operations = await api<OperationState[]>(page, "GET", `${base}/operations`);
    expect(operations).toHaveLength(1);
    const expense = operations[0].latest_posting;
    if (expense.kind !== "expense") throw new Error("Expected fictional split expense");
    expect(expense.amount).toBe("120.00");
    expect(expense.asset_id).toBe("CNY");
    expect(expense.splits).toEqual(
        categories.map((category, i) => ({
            category_id: category.id,
            amount: i ? "40.00" : "80.00",
        })),
    );
    const balances = await api<Balance[]>(page, "GET", `${base}/balances`);
    expect(balances).toHaveLength(1);
    expect(balances[0].amount).toBe("-120.00");
    const history = await api<HistoryEntry[]>(
        page,
        "GET",
        `${base}/operations/${expense.id}/history`,
    );
    const lines = history[0].journals[0].lines;
    expect(lines.filter((line) => line.role === "account")).toHaveLength(1);
    expect(lines.reduce((sum, line) => sum + parseAmount(line.amount, 2), 0n)).toBe(0n);
    expect(
        lines
            .filter((line) => line.role === "expense")
            .map((line) => [line.category_id, line.amount]),
    ).toEqual(categories.map((category, i) => [category.id, i ? "40.00" : "80.00"]));
    await page.getByTestId(`ocr-draft-${fixture.draft}`).click();
    await expect(page.getByText(/Original confirmation receipt/u)).toContainText("120.00 CNY");
});
