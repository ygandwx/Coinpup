import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account, Category } from "../src/ledger-api";
import type { BusinessPaymentDetail, BusinessPaymentReceipt } from "../src/business-settlement-api";

async function setup(page: Page, direction: "receipt" | "payout" = "receipt") {
    const data = await confirmationSetup(page, direction === "receipt" ? "invoice" : "bill");
    await api(page, "POST", `${data.base}/confirm`, {
        id: randomUUID(),
        expected_version: 1,
    });
    const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
        name: "Fictional settlement bank 中文",
        kind: "bank",
        asset_ids: ["USD"],
    });
    if (direction === "payout")
        await api(page, "POST", `${data.ledger}/opening-balances`, {
            account_id: account.id,
            asset_id: "USD",
            amount: "1000.00",
            transaction_date: "2026-01-01",
            description: "Fictional opening",
        });
    const categories = await api<Category[]>(page, "GET", `${data.ledger}/categories?limit=100`);
    const payment = await api<BusinessPaymentReceipt>(
        page,
        "POST",
        `${data.ledger}/business-payments`,
        {
            id: randomUUID(),
            direction,
            party_id: data.party.id,
            account_id: account.id,
            asset_id: "USD",
            amount: "110.00",
            transaction_date: "2026-01-02",
            description: "Fictional settlement history 中文 " + "long description ".repeat(12),
            fees: [
                {
                    account_id: account.id,
                    asset_id: "USD",
                    amount: "1.00",
                    category_id: categories.find((row) => row.kind === "expense")!.id,
                },
            ],
            initial_allocation: {
                id: randomUUID(),
                allocation_date: "2026-01-03",
                lines: [
                    {
                        document_id: data.source.id,
                        document_line_id: data.source.lines[0].id,
                        payment_amount: "40.00",
                        document_amount: "40.00",
                    },
                ],
            },
        },
    );
    await api(page, "POST", `${data.ledger}/business-allocations`, {
        id: randomUUID(),
        payment_id: payment.id,
        allocation_date: "2026-01-04",
        lines: [
            {
                document_id: data.source.id,
                document_line_id: data.source.lines[0].id,
                payment_amount: "30.00",
                document_amount: "30.00",
            },
        ],
    });
    await page.goto(`/?entity=${data.entity.id}&view=business-settlements`);
    return { ...data, account, payment };
}

for (const direction of ["receipt", "payout"] as const)
    test(`fictional ${direction} history keeps principal, fees and original allocation sides`, async ({
        page,
    }, info) => {
        test.setTimeout(90_000); // Bilingual screenshots at three widths.
        const data = await setup(page, direction);
        const panel = page.locator(".business-settlement-panel");
        await expect(panel.locator(".master-records > li")).toHaveCount(1);
        await expect(panel).toContainText("Unapplied: 40.00 USD");
        await panel.getByRole("button", { name: "View details", exact: true }).click();
        await expect(panel.locator(".confirmation-facts")).toContainText("110.00 USD");
        await expect(panel.locator(".confirmation-facts")).toContainText("70.00 USD");
        await expect(panel.locator(".confirmation-facts")).toContainText("40.00 USD");
        await expect(panel.locator(".master-records")).toContainText("1.00 USD");
        await expect(panel).toContainText("Fictional saved party 中文");
        if (direction === "receipt")
            for (const width of [1440, 375, 320])
                for (const locale of ["zh", "en"]) {
                    await page.setViewportSize({ width, height: 1000 });
                    await page
                        .getByRole("button", {
                            name: locale === "zh" ? "中文" : "English",
                            exact: true,
                        })
                        .click();
                    await expect(panel.locator(".confirmation-facts")).toContainText("110.00 USD");
                    await expectNoOverflow(page);
                    await page.screenshot({
                        path: info.outputPath(`settlement-history-${locale}-${width}.png`),
                        fullPage: true,
                    });
                }
        await panel
            .getByRole("button", {
                name: "View allocations for this payment",
                exact: true,
            })
            .click();
        await expect(panel.locator(".master-records > li")).toHaveCount(2);
        await expect(panel).toContainText("Showing allocations for the selected payment");
        await panel
            .locator(".master-records > li")
            .filter({ hasText: "Allocated later" })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(panel.locator(".confirmation-lines")).toContainText("Payment side: 30.00 USD");
        await expect(panel.locator(".confirmation-lines")).toContainText(
            "Document side: 30.00 USD",
        );
        await expect(panel.locator(".confirmation-lines")).toContainText(data.source.lines[0].id);
        await expectNoOverflow(page);
        await panel.getByRole("button", { name: "Back to settlement list" }).click();
        await panel
            .locator(".master-records > li")
            .filter({ hasText: "Allocated with payment" })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(panel.locator(".confirmation-lines")).toContainText("Payment side: 40.00 USD");
        const current = await api<BusinessPaymentDetail>(
            page,
            "GET",
            `${data.ledger}/business-payments/${data.payment.id}`,
        );
        expect(current.receipt).toEqual(data.payment);
        expect(current).toMatchObject({
            amount: "110.00",
            applied_amount: "70.00",
            unapplied_amount: "40.00",
        });
    });

test("fictional history preserves archived references and recovers a failed page without a write", async ({
    page,
}) => {
    const data = await setup(page);
    await api(page, "PATCH", `${data.ledger}/business-parties/${data.party.id}`, {
        expected_version: 1,
        archived: true,
    });
    await api(page, "PATCH", `${data.ledger}/accounts/${data.account.id}`, {
        expected_version: 1,
        archived: true,
    });
    let writes = 0,
        unavailable = true,
        failures = 0;
    page.on("request", (request) => {
        if (request.url().includes("business-payments") && request.method() !== "GET") writes++;
    });
    await page.route(`**${data.ledger}/business-payments?*`, async (route) => {
        // The old page can finish mounting while reload starts; keep the outage
        // active until the new page has visibly reported it.
        if (unavailable) {
            failures++;
            await route.fulfill({
                status: 503,
                json: { detail: { code: "business_unavailable" } },
            });
        } else await route.continue();
    });
    await page.reload();
    const panel = page.locator(".business-settlement-panel");
    await expect(panel.getByRole("alert")).toBeVisible();
    expect(failures).toBeGreaterThan(0);
    unavailable = false;
    await panel.getByRole("button", { name: "Refresh list" }).click();
    await panel.getByRole("button", { name: "View details" }).click();
    await expect(panel.getByRole("alert")).toHaveCount(0);
    await expect(panel).toContainText("Fictional saved party 中文");
    await expect(panel).toContainText(data.account.name);
    expect(writes).toBe(0);
});

test("fictional settlement pages retain exact long amounts and return from an empty allocation list", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await setup(page);
    for (let index = 0; index < 25; index++)
        await api(page, "POST", `${data.ledger}/business-payments`, {
            id: randomUUID(),
            direction: "receipt",
            party_id: data.party.id,
            account_id: data.account.id,
            asset_id: "USD",
            amount: index === 24 ? "999999999999999999.99" : "1.00",
            transaction_date: "2026-01-02",
            description: `Fictional pagination ${index}`,
        });
    await page.reload();
    const panel = page.locator(".business-settlement-panel");
    await expect(panel.locator(".master-records > li")).toHaveCount(25);
    await panel.getByRole("button", { name: "Next page" }).click();
    await expect(panel.locator(".master-records > li")).toHaveCount(1);
    await expect(panel).toContainText("999,999,999,999,999,999.99 USD");
    await expect(panel.getByRole("button", { name: "Next page" })).toBeDisabled();
    await page.setViewportSize({ width: 320, height: 1000 });
    await expectNoOverflow(page);
    await panel.getByRole("button", { name: "View details" }).click();
    await panel.getByRole("button", { name: "View allocations for this payment" }).click();
    await expect(panel.getByText("No settlements on this page.", { exact: true })).toBeVisible();
    await panel.getByRole("button", { name: "All allocations", exact: true }).click();
    await expect(panel.locator(".master-records > li")).toHaveCount(2);
    await panel.getByRole("button", { name: "Payments", exact: true }).click();
    await expect(panel.locator(".master-records > li")).toHaveCount(25);
    await expect(panel.getByRole("button", { name: "Previous page" })).toBeDisabled();
});
