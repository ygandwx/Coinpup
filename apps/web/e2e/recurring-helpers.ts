import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { expect, type Page } from "@playwright/test";
import { api, fictionalName, login } from "./helpers";
import type { Entity, Category } from "../src/ledger-api";
import type { Party } from "../src/business-api";
import type { BusinessDraft } from "../src/business-draft-api";

export async function recurringSetup(page: Page, date = new Date().toISOString().slice(0, 10)) {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("recurring drafts"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const ledger = `/api/v1/ledgers/${entity.ledger.id}`;
    const party = await api<Party>(page, "POST", `${ledger}/business-parties`, {
        id: randomUUID(),
        name: "Fictional recurring customer 中文",
        role: "customer",
    });
    const categories = await api<Category[]>(page, "GET", `${ledger}/categories?limit=100`);
    const source = await api<BusinessDraft>(page, "POST", `${ledger}/business-documents`, {
        id: randomUUID(),
        document_kind: "invoice",
        party_id: party.id,
        asset_id: "USD",
        issue_date: date,
        notes: "Fictional captured notes 原模板",
        lines: [
            {
                id: randomUUID(),
                description: "Fictional recurring service 中文",
                quantity: "1.00",
                unit_price: "100.00",
                tax_rate_percent: "0.00",
                category_id: categories.find((row) => row.kind === "income")!.id,
                recognition_date: date,
            },
        ],
    });
    await page.goto(`/?entity=${entity.id}&view=recurring`);
    return { entity, ledger, source, date, base: `${ledger}/recurring-invoice-rules` };
}

export async function fillRecurring(page: Page, name: string, date: string) {
    await page.getByRole("button", { name: "New recurring rule", exact: true }).click();
    await page.getByLabel("Rule name", { exact: true }).fill(name);
    await page.getByLabel("Rule timezone", { exact: true }).fill("Asia/Shanghai");
    await page.getByLabel("First occurrence date", { exact: true }).fill(date);
    await page.getByTestId("recurring-source").click();
    await expect(page.getByTestId("recurring-source-selected")).toContainText("100.00 USD");
}

export function runRecurringJob() {
    const result = JSON.parse(
        execFileSync(
            "docker",
            [
                "compose",
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "coinpup_api.jobs",
                "recurring-invoices",
                "--per-rule",
                "1",
            ],
            { encoding: "utf8", timeout: 30_000 },
        ),
    );
    expect(result.failures).toBe(0);
    return result;
}
