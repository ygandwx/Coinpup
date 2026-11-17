import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";
import { api } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account } from "../src/ledger-api";
import type { BusinessDraft } from "../src/business-draft-api";
import type { BusinessPaymentReceipt } from "../src/business-settlement-api";
import type {
    AllocationReturnCapacity,
    AllocationReturnReceipt,
} from "../src/allocation-return-api";

export async function returnRevisionFixture(page: Page) {
    const data = await confirmationSetup(page, "invoice", "100.00");
    const doc = await api<BusinessDraft>(page, "POST", data.ledger + "/business-documents", {
        id: randomUUID(),
        document_kind: "invoice",
        party_id: data.party.id,
        asset_id: "EUR",
        issue_date: "2026-01-02",
        lines: data.source.lines.map((line) => ({
            id: randomUUID(),
            description: line.description,
            quantity: "1.00",
            unit_price: "100.00",
            tax_rate_percent: "0",
            category_id: line.category_id,
            recognition_date: line.recognition_date,
        })),
    });
    await api(page, "POST", data.ledger + "/business-documents/" + doc.id + "/confirm", {
        id: randomUUID(),
        expected_version: doc.version,
    });
    const bank = await api<Account>(page, "POST", data.ledger + "/accounts", {
        name: "Fictional return revision bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const allocation = randomUUID();
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
            amount: "220.00",
            transaction_date: "2026-01-03",
            description: "Fictional return revision sources 中文",
            initial_allocation: {
                id: allocation,
                allocation_date: "2026-01-03",
                lines: doc.lines.map((line) => ({
                    document_id: doc.id,
                    document_line_id: line.id,
                    payment_amount: "110.00",
                    document_amount: "100.00",
                })),
            },
        },
    );
    const capacity = await api<AllocationReturnCapacity>(
        page,
        "GET",
        data.ledger + "/business-allocations/" + allocation + "/return-capacity",
    );
    const lines = capacity.lines.map((row) => ({
        source_allocation_line_id: row.source_allocation_line_id,
        expected_version: 1,
        payment_amount: "11.00",
        document_amount: "10.00",
    }));
    const original = await api<AllocationReturnReceipt>(
        page,
        "POST",
        data.ledger + "/business-allocation-returns",
        {
            id: randomUUID(),
            payment_id: payment.id,
            return_date: "2026-01-04",
            reason: "Fictional original return 中文",
            lines: [lines[0]],
        },
    );
    return {
        ...data,
        bank,
        payment,
        original,
        lines,
        path: data.ledger + "/business-allocation-returns/" + original.id,
    };
}
export async function openReturnVersions(
    page: Page,
    entityId: string,
    locale: "en" | "zh",
    reason?: string,
) {
    const en = locale === "en";
    await page.goto(`/?entity=${entityId}&view=business-settlements`);
    await page.getByRole("button", { name: en ? "English" : "中文", exact: true }).click();
    await page
        .getByRole("button", {
            name: en ? "Allocation return history" : "分摊退回历史",
            exact: true,
        })
        .click();
    const source =
        reason === undefined ? page : page.getByRole("listitem").filter({ hasText: reason });
    await source
        .getByRole("button", { name: en ? "View return details" : "查看退回详情", exact: true })
        .click();
    return page.getByRole("region", {
        name: en ? "Return group versions" : "退回组版本",
        exact: true,
    });
}
