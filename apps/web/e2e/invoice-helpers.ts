import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";
import { api, login, fictionalName } from "./helpers";
import type { Entity, Category } from "../src/ledger-api";
import type { Party } from "../src/business-api";
import type { BusinessDraft } from "../src/business-draft-api";
import type { BusinessConfirmationReceipt } from "../src/business-confirmation-api";
import type { InvoiceProfile } from "../src/invoice-issuance-receipt";
export async function invoiceSetup(page: Page) {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const company = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "company",
        name: fictionalName("Invoice"),
        country_code: "HK",
        company_type: "private_limited",
        base_asset_id: "USD",
        locale: "en",
    });
    const ledger = `/api/v1/ledgers/${company.ledger.id}`;
    const category = await api<Category>(page, "POST", ledger + "/categories", {
        name: fictionalName("income"),
        kind: "income",
    });
    const customer = await api<Party>(page, "POST", ledger + "/business-parties", {
        id: randomUUID(),
        name: "Fictional 客户",
        role: "customer",
    });
    const doc = await api<BusinessDraft>(page, "POST", ledger + "/business-documents", {
        id: randomUUID(),
        document_kind: "invoice",
        party_id: customer.id,
        asset_id: "USD",
        issue_date: "2026-11-14",
        due_date: "2026-12-14",
        lines: [
            {
                id: randomUUID(),
                description: "Fictional 双语服务",
                quantity: "3",
                unit_price: "19.99",
                discount_amount: "9.97",
                tax_rate_percent: "8.25",
                category_id: category.id,
                recognition_date: "2026-11-14",
            },
        ],
    });
    const confirmation = await api<BusinessConfirmationReceipt>(
        page,
        "POST",
        ledger + "/business-documents/" + doc.id + "/confirm",
        { id: randomUUID(), expected_version: doc.version },
    );
    const profile = await api<InvoiceProfile>(page, "POST", ledger + "/invoice-profiles", {
        id: randomUUID(),
        name: "Fictional display",
        numbering: { prefix: "示例-", minimum_digits: 4 },
        payment_instructions: "Fictional bank details only",
    });
    return { company, ledger, doc, confirmation, profile, category, customer };
}
