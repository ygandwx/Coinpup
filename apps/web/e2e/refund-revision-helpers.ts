import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";
import { api } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account } from "../src/ledger-api";
import type { BusinessPaymentReceipt } from "../src/business-settlement-api";
import type { RefundReceipt } from "../src/refund-api";

export async function refundRevisionFixture(page: Page) {
    const data = await confirmationSetup(page);
    const bank = await api<Account>(page, "POST", data.ledger + "/accounts", {
        name: "Fictional refund revision bank",
        kind: "bank",
        asset_ids: ["USD", "BTC"],
    });
    const payment = await api<BusinessPaymentReceipt>(
        page,
        "POST",
        data.ledger + "/business-payments",
        {
            id: randomUUID(),
            direction: "receipt",
            party_id: data.party.id,
            account_id: bank.id,
            asset_id: "USD",
            amount: "100.00",
            transaction_date: "2026-01-02",
            description: "Fictional original payment 中文",
            fees: [],
        },
    );
    const original = await api<RefundReceipt>(page, "POST", data.ledger + "/business-refunds", {
        id: randomUUID(),
        payment_id: payment.id,
        expected_version: 1,
        account_id: bank.id,
        asset_id: "USD",
        amount: "20.00",
        payment_amount: "20.00",
        transaction_date: "2026-01-03",
        reason: "Fictional first refund 中文",
        fees: [],
        allocation_return_ids: [],
    });
    return {
        ...data,
        bank,
        original,
        refundPath: data.ledger + "/business-refunds/" + original.id,
    };
}
export async function openRefundVersions(page: Page, entityId: string, locale: "en" | "zh") {
    const en = locale === "en";
    await page.goto(`/?entity=${entityId}&view=business-settlements`);
    await page.getByRole("button", { name: en ? "English" : "中文", exact: true }).click();
    await page
        .getByRole("button", { name: en ? "Actual refund history" : "实际退款历史", exact: true })
        .click();
    await page
        .getByRole("button", { name: en ? "View refund details" : "查看退款详情", exact: true })
        .click();
    await page
        .getByRole("button", {
            name: en ? "Current refund and version history" : "当前退款与版本历史",
            exact: true,
        })
        .click();
    return page.getByRole("region", { name: en ? "Refund versions" : "退款版本", exact: true });
}
