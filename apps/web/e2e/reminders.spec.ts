import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login } from "./helpers";
import type { Entity } from "../src/ledger-api";
import type { Reminder } from "../src/reminder-api";

test("fictional reminder dates preserve bilingual input, manual override and immutable history", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("reminder dates"),
        base_asset_id: "USD",
        locale: "en",
    });
    const ledger = `/api/v1/ledgers/${entity.ledger.id}`,
        base = `${ledger}/reminders`;
    await page.goto(`/?entity=${entity.id}&view=reminders`);
    await page.getByRole("button", { name: "New reminder event", exact: true }).click();
    await page.getByLabel("Event kind", { exact: true }).selectOption("certificate");
    await page.getByLabel("Event title", { exact: true }).fill("Fictional 证件期限");
    await page.getByLabel("Notes", { exact: true }).fill("Fictional evidence 示例");
    const rules = await api<{ id: string; version: string }[]>(page, "GET", `${base}/rules`);
    const rule = rules.find((row) => row.id === "certificate.expiry")!;
    await page
        .getByLabel("Calculation rule", { exact: true })
        .selectOption(JSON.stringify([rule.id, rule.version]));
    await page.getByLabel("Expiry date on certificate", { exact: true }).fill("2028-01-31");
    await page.getByLabel("Applicability to this event", { exact: true }).selectOption("yes");
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 1000 });
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(
                page.getByLabel(locale === "zh" ? "事项标题" : "Event title", { exact: true }),
            ).toHaveValue("Fictional 证件期限");
            await expect(
                page.getByLabel(locale === "zh" ? "证件记载到期日" : "Expiry date on certificate", {
                    exact: true,
                }),
            ).toHaveValue("2028-01-31");
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`reminder-form-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    await page.getByRole("button", { name: "Create event", exact: true }).click();
    await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    const rows = await api<Reminder[]>(page, "GET", base);
    expect(rows).toHaveLength(1);
    const path = `${base}/${rows[0].id}`;
    expect(rows[0].effective_date).toBe("2028-01-31");
    await page.getByRole("button", { name: "View saved event", exact: true }).click();
    await page.getByRole("button", { name: "Set manual date", exact: true }).click();
    await page.getByLabel("Manual date", { exact: true }).fill("2028-02-10");
    await page
        .getByLabel("Reason for this manual date action", { exact: true })
        .fill("Fictional reviewed extension 人工核验");
    await page.getByRole("button", { name: "Set manual date", exact: true }).click();
    await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "View saved event", exact: true }).click();
    await page.getByRole("button", { name: "Recalculate explicitly", exact: true }).click();
    await page.getByLabel("Expiry date on certificate", { exact: true }).fill("2028-03-01");
    await page.getByRole("button", { name: "Recalculate selected rule", exact: true }).click();
    await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    const changed = await api<Reminder>(page, "GET", path);
    expect(changed.version).toBe(3);
    expect(changed.calculated_date).toBe("2028-03-01");
    expect(changed.effective_date).toBe("2028-02-10");
    expect(changed.manual_reason).toBe("Fictional reviewed extension 人工核验");
    await page.getByRole("button", { name: "View saved event", exact: true }).click();
    const history = page.getByRole("region", { name: "Event revision history", exact: true });
    await expect(history.getByText("Version 3", { exact: false }).first()).toBeVisible();
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 1000 });
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`reminder-history-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    await page.getByRole("button", { name: "Clear manual date", exact: true }).click();
    await expect(
        page.getByLabel("Reason for this manual date action", { exact: true }),
    ).toHaveValue("");
    await page
        .getByLabel("Reason for this manual date action", { exact: true })
        .fill("Fictional accepted recalculation");
    await page.getByRole("button", { name: "Clear manual date", exact: true }).click();
    await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    expect((await api<Reminder>(page, "GET", path)).effective_date).toBe("2028-03-01");
    for (const action of ["Mark completed", "Reopen event", "Archive event", "Restore event"]) {
        await page.getByRole("button", { name: "View saved event", exact: true }).click();
        await page.getByRole("button", { name: action, exact: true }).click();
        await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    }
    const revisions = await api<{ version: number; snapshot: Reminder }[]>(
        page,
        "GET",
        `${path}/revisions`,
    );
    expect(revisions.map((row) => row.version)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(revisions[0].snapshot.calculated_date).toBe("2028-01-31");
    expect(revisions[1].snapshot.manual_due_date).toBe("2028-02-10");
    expect(await api(page, "GET", `${ledger}/operations`)).toEqual([]);
});
