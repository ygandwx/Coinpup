import { seed } from "./ocr-helpers";
import { expect, test } from "@playwright/test";
import { expectNoOverflow, login } from "./helpers";

test("fictional seeded OCR review uses real API and keeps selections across languages and widths", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed();
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await expect(page.getByRole("heading", { name: "Recognition tasks" })).toBeVisible();
    await page.locator(`[data-job-id="${fixture.job}"]`).click();
    await page.getByRole("button", { name: /^Draft 1 · Review needed$/u }).click();
    await expect(
        page.getByText("Fictional seeded browser candidate: Total USD 10.00", { exact: true }),
    ).toBeVisible();
    const canvas = page.getByRole("img", { name: "Original page 1", exact: true });
    await expect(canvas).toBeVisible();
    expect(
        await canvas.evaluate((element) => {
            const canvas = element as HTMLCanvasElement;
            const pixels = canvas
                .getContext("2d")!
                .getImageData(0, 0, canvas.width, canvas.height).data;
            let ink = 0;
            for (let i = 0; i < pixels.length; i += 4)
                if (pixels[i] < 220 && pixels[i + 3] > 0) ink++;
            return ink;
        }),
    ).toBeGreaterThan(20);
    await page.getByRole("button", { name: "Next original page", exact: true }).click();
    await expect(page.getByRole("img", { name: "Original page 2", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Previous original page", exact: true }).click();
    await expect(canvas).toBeVisible();
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

test("fictional photo original is rendered without embedding or persistent private storage", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed(true);
    const external: string[] = [];
    const origin = new URL(page.url()).origin;
    page.on("request", (request) => {
        if (request.url().startsWith("http") && new URL(request.url()).origin !== origin)
            external.push(request.url());
    });
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await page.getByRole("button", { name: /^Draft 1 · Review needed$/u }).click();
    await expect(page.getByRole("img", { name: "Original page 1", exact: true })).toBeVisible();
    await page.setViewportSize({ width: 375, height: 1000 });
    await expectNoOverflow(page);
    await page.screenshot({
        path: info.outputPath("fictional-photo-preview-375.png"),
        fullPage: true,
    });
    expect(await page.locator("iframe,embed,object").count()).toBe(0);
    expect(await page.evaluate(() => Object.keys(localStorage))).toEqual(["coinpup.locale"]);
    expect(await page.evaluate(() => Object.keys(sessionStorage))).toEqual([]);
    expect(external).toEqual([]);
});
