import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login } from "./helpers";
import type { Entity, Category } from "../src/ledger-api";
import type { Party, Project } from "../src/business-api";
import type { BusinessDraft, BusinessDraftSummary } from "../src/business-draft-api";

async function setup(page: Page) {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("business drafts"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const ledger = `/api/v1/ledgers/${entity.ledger.id}`;
    const party = await api<Party>(page, "POST", `${ledger}/business-parties`, {
        id: randomUUID(),
        name: "Fictional customer 中文",
        role: "both",
    });
    const project = await api<Project>(page, "POST", `${ledger}/business-projects`, {
        id: randomUUID(),
        name: "Fictional project 中文",
    });
    const categories = await api<Category[]>(page, "GET", `${ledger}/categories?limit=100`);
    await page.goto(`/?entity=${entity.id}&view=drafts`);
    return {
        entity,
        party,
        project,
        categories,
        ledger,
        base: `${ledger}/business-documents`,
    };
}
test("fictional invoice draft preserves bilingual exact inputs, computes line rounding and saves editable snapshots", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await setup(page);
    await page.getByRole("button", { name: "New business draft", exact: true }).click();
    await page.getByLabel("Business party", { exact: true }).selectOption(data.party.id);
    await page.getByLabel("Pricing asset", { exact: true }).selectOption("USD");
    await page.getByLabel("Issue date", { exact: true }).fill("2026-01-01");
    await page.getByRole("button", { name: "Add line", exact: true }).click();
    await page
        .getByLabel("Description", { exact: true })
        .fill(" Fictional multilingual consulting 中文 ");
    await page.getByLabel("Quantity", { exact: true }).fill("2.50");
    await page.getByLabel("Unit price before tax", { exact: true }).fill("10.01");
    await page.getByLabel("Fixed line discount", { exact: true }).fill("0.02");
    await page.getByLabel("Tax rate (%)", { exact: true }).fill("8.25");
    await page
        .getByLabel("Category", { exact: true })
        .selectOption(data.categories.find((row) => row.kind === "income")!.id);
    await page.getByLabel("Project (optional)", { exact: true }).selectOption(data.project.id);
    await page.getByLabel("Recognition date", { exact: true }).fill("2025-12-31");
    await page.getByLabel("Notes", { exact: true }).fill("  Fictional notes  ");
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 1000 });
            await page
                .getByRole("button", {
                    name: locale === "zh" ? "中文" : "English",
                    exact: true,
                })
                .click();
            await expect(
                page.getByLabel(locale === "zh" ? "数量" : "Quantity", { exact: true }),
            ).toHaveValue("2.50");
            await expect(
                page.getByLabel(locale === "zh" ? "备注" : "Notes", { exact: true }),
            ).toHaveValue("  Fictional notes  ");
            await expect(page.locator(".draft-totals")).toContainText("27.07 USD");
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`business-draft-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    await page.getByLabel("Pricing asset", { exact: true }).selectOption("GBP");
    await expect(page.getByLabel("Unit price before tax", { exact: true })).toHaveValue("10.01");
    await expect(page.locator(".draft-totals")).toContainText("27.07 GBP");
    await page.getByLabel("Pricing asset", { exact: true }).selectOption("USD");
    for (const [label, value] of [
        ["Quantity", "99999999999999999999"],
        ["Unit price before tax", "0.01"],
        ["Fixed line discount", "0"],
        ["Tax rate (%)", "0"],
    ])
        await page.getByLabel(label, { exact: true }).fill(value);
    await expect(page.locator(".draft-totals")).toContainText("999,999,999,999,999,999.99 USD");
    await expectNoOverflow(page);
    await page.screenshot({ path: info.outputPath("business-draft-long-320.png"), fullPage: true });
    for (const [label, value] of [
        ["Quantity", "2.50"],
        ["Unit price before tax", "10.01"],
        ["Fixed line discount", "0.02"],
        ["Tax rate (%)", "8.25"],
    ])
        await page.getByLabel(label, { exact: true }).fill(value);
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page.getByText("Draft saved and verified.", { exact: true })).toBeVisible();
    let rows = await api<BusinessDraftSummary[]>(page, "GET", data.base);
    expect(rows).toHaveLength(1);
    let detail = await api<BusinessDraft>(page, "GET", `${data.base}/${rows[0].id}`);
    expect(detail).toMatchObject({
        net_amount: "25.01",
        tax_amount: "2.06",
        total_amount: "27.07",
        version: 1,
        notes: "  Fictional notes  ",
    });
    expect(detail.lines[0]).toMatchObject({
        quantity: "2.50",
        recognition_date: "2025-12-31",
        project_id: data.project.id,
    });
    await api(page, "PATCH", `${data.ledger}/business-parties/${data.party.id}`, {
        expected_version: 1,
        name: "Fictional renamed customer",
    });
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await expect(page.getByText("Saved issuer / party snapshots", { exact: false })).toContainText(
        "Fictional customer 中文",
    );
    await page.getByLabel("Quantity", { exact: true }).fill("3.00");
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    detail = await api<BusinessDraft>(page, "GET", `${data.base}/${rows[0].id}`);
    expect(detail.version).toBe(2);
    expect(detail.party_snapshot.name).toBe("Fictional customer 中文");
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await page
        .getByLabel("Refresh issuer, party and category/project snapshots when saving", {
            exact: true,
        })
        .check();
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    detail = await api<BusinessDraft>(page, "GET", `${data.base}/${rows[0].id}`);
    expect(detail.party_snapshot.name).toBe("Fictional renamed customer");
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await page.getByRole("button", { name: "Archive draft", exact: true }).click();
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await expect(page.getByRole("button", { name: "Save draft", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Restore draft", exact: true }).click();
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    rows = await api<BusinessDraftSummary[]>(page, "GET", data.base);
    expect(rows[0]).toMatchObject({ version: 5, archived: false });
    expect(await api(page, "GET", `${data.ledger}/operations`)).toEqual([]);
});

test("fictional bill draft validates input, preserves empty drafts and recovers list read failures", async ({
    page,
}) => {
    const data = await setup(page);
    await page.getByRole("button", { name: "New business draft", exact: true }).click();
    await page.getByLabel("Document type", { exact: true }).selectOption("bill");
    await page.getByLabel("Business party", { exact: true }).selectOption(data.party.id);
    await page.getByLabel("Pricing asset", { exact: true }).selectOption("USD");
    await page.getByRole("button", { name: "Add line", exact: true }).click();
    await page.getByLabel("Description", { exact: true }).fill("Fictional bill");
    await page
        .getByLabel("Category", { exact: true })
        .selectOption(data.categories.find((row) => row.kind === "expense")!.id);
    await page.getByLabel("Unit price before tax", { exact: true }).fill("1.001");
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("Check active party");
    await expect(page.getByLabel("Unit price before tax", { exact: true })).toHaveValue("1.001");
    await page.getByRole("button", { name: "Remove line", exact: true }).click();
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    const rows = await api<BusinessDraftSummary[]>(page, "GET", data.base);
    expect(rows[0]).toMatchObject({
        document_kind: "bill",
        line_count: 0,
        total_amount: "0.00",
    });
    await page.route(`**${data.base}?*`, (route) =>
        route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
    );
    await page.getByRole("button", { name: "Refresh drafts", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await page.unroute(`**${data.base}?*`);
    await page.getByRole("button", { name: "Refresh drafts", exact: true }).click();
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await expect(page.getByLabel("Document type", { exact: true })).toHaveValue("bill");
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    await api(page, "PATCH", `/api/v1/entities/${data.entity.id}`, {
        expected_version: 1,
        archived: true,
    });
    await page.reload();
    await expect(
        page.getByRole("button", { name: "New business draft", exact: true }),
    ).toBeDisabled();
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await expect(page.getByRole("button", { name: "Save draft", exact: true })).toBeDisabled();
});
