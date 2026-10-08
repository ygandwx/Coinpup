import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { expectNoOverflow, login } from "./helpers";

test("fictional seeded OCR review uses real API and keeps selections across languages and widths", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = JSON.parse(
        execFileSync(
            "docker",
            [
                "compose",
                "exec",
                "-T",
                "-e",
                "COINPUP_ENVIRONMENT=test",
                "-e",
                "COINPUP_CREATE_OCR_BROWSER_FIXTURE=1",
                "api",
                "python",
                "scripts/create_ocr_browser_fixture.py",
            ],
            { encoding: "utf8" },
        ),
    );
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await expect(page.getByRole("heading", { name: "Recognition tasks" })).toBeVisible();
    await page.locator(`[data-job-id="${fixture.job}"]`).click();
    await page.getByRole("button", { name: /^Draft 1 · Review needed$/u }).click();
    await expect(
        page.getByText("Fictional seeded browser candidate: Total USD 10.00", { exact: true }),
    ).toBeVisible();
    const total = page.getByLabel("Total · Reviewed", { exact: true });
    await total.check();
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(
                page.getByLabel(language === "zh" ? "合计 · 已核对" : "Total · Reviewed", {
                    exact: true,
                }),
            ).toBeChecked();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-review-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    const saved = page.waitForResponse(
        (response) =>
            response.url().endsWith(`/${fixture.draft}/review`) &&
            response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "Save review", exact: true }).click();
    expect((await saved).status()).toBe(200);
    const state = await page.request.get(
        `/api/v1/ledgers/${fixture.ledger}/ocr-drafts/${fixture.draft}/review`,
    );
    expect((await state.json()).review.confirmed).toEqual(["header.total"]);
    await page.getByRole("button", { name: "Ignore draft", exact: true }).click();
    await expect(page.getByRole("button", { name: "Restore draft", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Restore draft", exact: true }).click();
    await expect(page.getByRole("button", { name: "Ignore draft", exact: true })).toBeVisible();
    const operations = await page.request.get(`/api/v1/ledgers/${fixture.ledger}/operations`);
    expect(await operations.json()).toEqual([]);
});
