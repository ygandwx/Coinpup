import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";
import { api, fictionalName, login } from "./helpers";
import type { Entity, Category } from "../src/ledger-api";
import type { Party } from "../src/business-api";
import type { BusinessDraft } from "../src/business-draft-api";

export async function confirmationSetup(
    page: Page,
    kind: "invoice" | "bill" = "invoice",
    price = "100.00",
) {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("confirmation"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const ledger = `/api/v1/ledgers/${entity.ledger.id}`;
    const party = await api<Party>(page, "POST", `${ledger}/business-parties`, {
        id: randomUUID(),
        name: "Fictional saved party 中文",
        role: "both",
    });
    const categories = await api<Category[]>(page, "GET", `${ledger}/categories?limit=100`);
    const category = categories.find(
        (row) => row.kind === (kind === "invoice" ? "income" : "expense"),
    )!;
    const source = await api<BusinessDraft>(page, "POST", `${ledger}/business-documents`, {
        id: randomUUID(),
        document_kind: kind,
        party_id: party.id,
        asset_id: "USD",
        issue_date: "2026-01-02",
        due_date: "2026-02-01",
        notes: "Fictional saved review only 中文",
        lines: [price, "0.00"].map((price, index) => ({
            id: randomUUID(),
            description: `Fictional service 中文 ${index}`,
            quantity: "1.00",
            unit_price: price,
            tax_rate_percent: "10.00",
            category_id: category.id,
            recognition_date: index === 0 ? "2026-01-01" : "2026-01-02",
        })),
    });
    await page.goto(`/?entity=${entity.id}&view=business-confirmations`);
    return { entity, ledger, party, source, base: `${ledger}/business-documents/${source.id}` };
}
