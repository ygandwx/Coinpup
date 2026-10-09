import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { fillRecurring, recurringSetup, runRecurringJob } from "./recurring-helpers";
import type { RecurringRule, RecurringInstance } from "../src/recurring-api";
import type { BusinessDraft } from "../src/business-draft-api";

test("fictional monthly rules preserve bilingual input and generate one editable draft per occurrence", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await recurringSetup(page);
    await fillRecurring(page, "Fictional 每月100USD", data.date);
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 1000 });
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(
                page.getByLabel(locale === "zh" ? "规则名称" : "Rule name", { exact: true }),
            ).toHaveValue("Fictional 每月100USD");
            await expect(
                page.getByLabel(locale === "zh" ? "规则时区" : "Rule timezone", { exact: true }),
            ).toHaveValue("Asia/Shanghai");
            await expect(page.getByTestId("recurring-source-selected")).toContainText("100.00 USD");
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`recurring-form-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    await page.getByRole("button", { name: "Save recurring rule", exact: true }).click();
    await expect(page.getByText("Rule saved and verified.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Back to rules", exact: true }).click();
    const rules = await api<RecurringRule[]>(page, "GET", data.base);
    expect(rules).toHaveLength(1);
    const rule = rules[0];
    expect(rule.source_document_id).toBe(data.source.id);
    expect(rule.template_input.lines![0].unit_price).toBe("100.00");
    // The UTC date is already due in Asia/Shanghai, with the next month still in the future.
    // Another browser's concurrent bounded job can confirm the same occurrence safely.
    runRecurringJob();
    runRecurringJob();
    const instances = await api<RecurringInstance[]>(
        page,
        "GET",
        `${data.base}/${rule.id}/instances`,
    );
    expect(instances).toHaveLength(1);
    expect(instances[0].scheduled_date).toBe(data.date);
    expect(await api(page, "GET", `${data.ledger}/operations`)).toEqual([]);
    await page.getByRole("button", { name: "Refresh rules", exact: true }).click();
    await expect(page.getByTestId("recurring-rule")).toContainText("Generated 1");
    await page.getByRole("button", { name: "View recurring rule", exact: true }).click();
    await expect(page.getByTestId("recurring-instance")).toHaveCount(1);
    await page.getByText("Captured template input", { exact: true }).click();
    const captured = page
        .locator("details")
        .filter({ has: page.getByText("Captured template input", { exact: true }) });
    await expect(
        captured.getByText("Fictional captured notes 原模板", { exact: true }),
    ).toBeVisible();
    const original = instances[0].original_input;
    const editable = Object.fromEntries(Object.entries(original).filter(([key]) => key !== "id"));
    await api(page, "PUT", `${data.ledger}/business-documents/${instances[0].id}`, {
        ...editable,
        expected_version: 1,
        notes: "Fictional edited current draft 人工修改",
    });
    await page.getByRole("button", { name: "View current draft", exact: true }).click();
    await expect(page.getByLabel("Notes", { exact: true })).toHaveValue(
        "Fictional edited current draft 人工修改",
    );
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    await page
        .getByRole("navigation", { name: "Workspace navigation" })
        .getByRole("button", { name: "Recurring invoices", exact: true })
        .click();
    await page.getByRole("button", { name: "View recurring rule", exact: true }).click();
    const item = page.getByTestId("recurring-instance");
    await item.getByText("Original generated input", { exact: true }).click();
    await expect(item).toContainText("Fictional captured notes 原模板");
    await expect(item).not.toContainText("Fictional edited current draft");
    for (const locale of ["zh", "en"]) {
        await page
            .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
            .click();
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`recurring-instance-${locale}-320.png`),
            fullPage: true,
        });
    }
    await page.getByRole("button", { name: "Pause rule", exact: true }).click();
    await expect(page.getByText("Rule saved and verified.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Back to rules", exact: true }).click();
    await expect(page.getByTestId("recurring-rule")).toContainText("Paused");
    runRecurringJob();
    expect(await api(page, "GET", `${data.base}/${rule.id}/instances`)).toEqual(instances);
    const current = await api<BusinessDraft>(
        page,
        "GET",
        `${data.ledger}/business-documents/${instances[0].id}`,
    );
    expect(current.notes).toBe("Fictional edited current draft 人工修改");
    expect(await api(page, "GET", `${data.ledger}/operations`)).toEqual([]);
});
