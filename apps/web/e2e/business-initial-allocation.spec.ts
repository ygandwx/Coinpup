import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account, Balance } from "../src/ledger-api";
import type { BusinessDraft } from "../src/business-draft-api";
import type {
    BusinessPaymentDetail,
    BusinessPaymentSummary,
    BusinessPaymentCreate,
} from "../src/business-settlement-api";

async function setup(page: Page, cross: boolean) {
    const data = await confirmationSetup(page, cross ? "bill" : "invoice");
    const source = await api<BusinessDraft>(page, "POST", `${data.ledger}/business-documents`, {
        id: randomUUID(),
        document_kind: cross ? "bill" : "invoice",
        party_id: data.party.id,
        asset_id: cross ? "EUR" : "USD",
        issue_date: "2026-09-30",
        notes: "Fictional initial allocation only 中文",
        lines: ["100.00", "0.00"].map((price, index) => ({
            id: randomUUID(),
            description: `Fictional allocation source 中文 ${index}`,
            quantity: "1.00",
            unit_price: price,
            tax_rate_percent: cross ? "0.00" : "10.00",
            category_id: data.source.lines[0].category_id,
            recognition_date: "2026-09-30",
        })),
    });
    await api(page, "POST", `${data.ledger}/business-documents/${source.id}/confirm`, {
        id: randomUUID(),
        expected_version: 1,
    });
    const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
        name: "Fictional initial allocation bank 中文",
        kind: "bank",
        asset_ids: ["USD"],
    });
    await api(page, "POST", `${data.ledger}/opening-balances`, {
        account_id: account.id,
        asset_id: "USD",
        amount: "1000.00",
        transaction_date: "2026-01-01",
        description: "Fictional opening only",
    });
    await page.goto(`/?entity=${data.entity.id}&view=business-settlements`);
    await page.getByRole("button", { name: "New payment", exact: true }).click();
    await page
        .getByLabel("Payment direction", { exact: true })
        .selectOption(cross ? "payout" : "receipt");
    await page.getByLabel("Business party", { exact: true }).selectOption(data.party.id);
    await page.getByLabel("Money account", { exact: true }).selectOption(account.id);
    await page.getByLabel("Principal asset", { exact: true }).selectOption("USD");
    await page.getByLabel("Principal", { exact: true }).fill("110.00");
    await page.getByLabel("Transaction date", { exact: true }).fill("2026-09-15");
    if (cross) {
        await page.getByRole("button", { name: "Add fee", exact: true }).click();
        await page.getByLabel("Fee account", { exact: true }).selectOption(account.id);
        await page.getByLabel("Fee asset", { exact: true }).selectOption("USD");
        await page.getByLabel("Fee amount", { exact: true }).fill("1.00");
        await page
            .getByLabel("Fee category", { exact: true })
            .selectOption(data.source.lines[0].category_id!);
    }
    await page
        .getByRole("checkbox", { name: "Allocate to confirmed documents atomically" })
        .check();
    await page.getByRole("button", { name: "Choose document lines", exact: true }).click();
    await expect(page.getByRole("button", { name: "Select this line", exact: true })).toHaveCount(
        1,
    );
    await page.getByRole("button", { name: "Select this line", exact: true }).click();
    return { ...data, source, account };
}

for (const cross of [false, true])
    test(`fictional initial ${cross ? "cross-asset payout" : "same-asset receipt"} is one atomic intent`, async ({
        page,
    }, info) => {
        test.setTimeout(90_000);
        const data = await setup(page, cross);
        const paymentQuantity = page.getByLabel(
            cross ? "Payment quantity (USD)" : "Allocation amount (USD)",
            { exact: true },
        );
        await paymentQuantity.fill("44.00");
        if (cross) await page.getByLabel("Document quantity (EUR)", { exact: true }).fill("40.00");
        await expect(page.getByLabel("Business party", { exact: true })).toBeDisabled();
        await expect(page.getByLabel("Principal asset", { exact: true })).toBeDisabled();
        await expect(page.getByLabel("Payment direction", { exact: true })).toBeDisabled();
        await page.getByLabel("Allocation date", { exact: true }).fill("2026-09-29");
        const submitted: BusinessPaymentCreate[] = [];
        let allocationPosts = 0;
        await page.route(`**${data.ledger}/business-payments`, async (route) => {
            if (route.request().method() !== "POST") return route.continue();
            submitted.push(route.request().postDataJSON());
            await route.continue();
        });
        page.on("request", (request) => {
            if (request.method() === "POST" && request.url().endsWith("/business-allocations"))
                allocationPosts++;
        });
        await page.getByRole("checkbox", { name: "I reviewed direction", exact: false }).check();
        await page.getByRole("button", { name: "Record actual payment", exact: true }).click();
        await expect(page.locator(".business-settlement-panel").getByRole("alert")).toBeVisible();
        expect(submitted).toHaveLength(0);
        await page.getByLabel("Allocation date", { exact: true }).fill("2026-09-30");
        await paymentQuantity.fill("110.01");
        if (cross) await page.getByLabel("Document quantity (EUR)", { exact: true }).fill("100.01");
        await page.getByRole("checkbox", { name: "I reviewed direction", exact: false }).check();
        await page.getByRole("button", { name: "Record actual payment", exact: true }).click();
        expect(submitted).toHaveLength(0);
        await paymentQuantity.fill("44.00");
        if (cross) await page.getByLabel("Document quantity (EUR)", { exact: true }).fill("40.00");
        await page.getByRole("checkbox", { name: "I reviewed direction", exact: false }).check();
        for (const [locale, width] of [
            ["zh", 320],
            ["en", 375],
            ["en", 1440],
        ] as const) {
            await page.setViewportSize({ width, height: 1000 });
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`initial-${cross}-${locale}-${width}.png`),
                fullPage: true,
            });
        }
        await page
            .getByRole("button", { name: "Record actual payment", exact: true })
            .evaluate((button) => {
                (button as HTMLButtonElement).click();
                (button as HTMLButtonElement).click();
            });
        await expect(
            page.getByText("Saved; the complete original receipt is verified.", { exact: true }),
        ).toBeVisible();
        expect(submitted).toHaveLength(1);
        expect(allocationPosts).toBe(0);
        expect(submitted[0].initial_allocation).toMatchObject({
            allocation_date: "2026-09-30",
            lines: [
                {
                    document_id: data.source.id,
                    document_line_id: data.source.lines[0].id,
                    payment_amount: "44.00",
                    document_amount: cross ? "40.00" : "44.00",
                },
            ],
        });
        const detail = await api<BusinessPaymentDetail>(
            page,
            "GET",
            `${data.ledger}/business-payments/${submitted[0].id}`,
        );
        expect(detail).toMatchObject({
            amount: "110.00",
            applied_amount: "44.00",
            unapplied_amount: "66.00",
        });
        expect(detail.receipt.initial_allocation?.postings).toHaveLength(1);
        const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
        expect(
            balances.find((row) => row.account_id === data.account.id && row.asset_id === "USD")
                ?.amount,
        ).toBe(cross ? "889.00" : "1110.00");
    });

test("fictional stale allocation capacity rejects the whole payment without a second cash movement", async ({
    page,
}) => {
    const data = await setup(page, false);
    await page.getByLabel("Allocation date", { exact: true }).fill("2026-09-30");
    await page.getByLabel("Allocation amount (USD)", { exact: true }).fill("110.00");
    const existing = (await api(page, "POST", `${data.ledger}/business-payments`, {
        id: randomUUID(),
        direction: "receipt",
        party_id: data.party.id,
        account_id: data.account.id,
        asset_id: "USD",
        amount: "110.00",
        transaction_date: "2026-09-30",
        initial_allocation: {
            id: randomUUID(),
            allocation_date: "2026-09-30",
            lines: [
                {
                    document_id: data.source.id,
                    document_line_id: data.source.lines[0].id,
                    payment_amount: "110.00",
                    document_amount: "110.00",
                },
            ],
        },
    })) as { id: string };
    await page.getByRole("checkbox", { name: "I reviewed direction", exact: false }).check();
    await page.getByRole("button", { name: "Record actual payment", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Edit rejected request", exact: true }),
    ).toBeVisible();
    const payments = await api<BusinessPaymentSummary[]>(
        page,
        "GET",
        `${data.ledger}/business-payments`,
    );
    expect(payments.map((row) => row.id)).toEqual([existing.id]);
    const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
    expect(
        balances.find((row) => row.account_id === data.account.id && row.asset_id === "USD")
            ?.amount,
    ).toBe("1110.00");
});
