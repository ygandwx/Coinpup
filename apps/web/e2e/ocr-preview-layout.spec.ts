import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, login } from "./helpers";
import { seed } from "./ocr-helpers";

for (const locale of ["zh", "en"] as const) {
    for (const outcome of ["ready", "failed"] as const) {
        test(`preview ${outcome} preserves an in-flight review click (${locale})`, async ({
            page,
        }, info) => {
            const width = info.project.name === "mobile-chromium" ? 320 : 1440;
            await page.setViewportSize({ width, height: 1000 });
            await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
            await login(page);
            // Statement rows expose Amount; the one-document fixture exposes Total.
            const fixture = seed(false, undefined, 3);
            let release = () => {};
            const gate = new Promise<void>((resolve) => {
                release = resolve;
            });
            await page.route(
                `**/files/${fixture.file}/content`,
                async (route) => {
                    await gate;
                    if (outcome === "failed") await route.abort("failed");
                    else await route.continue();
                },
                { times: 1 },
            );
            await page.goto(`/?entity=${fixture.entity}&view=ocr`);
            if (locale === "zh")
                await page.getByRole("button", { name: "中文", exact: true }).click();
            await page.getByTestId(`ocr-draft-${fixture.drafts[0]}`).click();
            const review = page.getByTestId(`ocr-review-${fixture.drafts[0]}`);
            await expect(review).toBeVisible();
            const label = (zh: string, en: string) =>
                `${locale === "zh" ? zh : en} · ${locale === "zh" ? "已核对" : "Reviewed"}`;
            const amount = review.getByLabel(label("金额", "Amount"), { exact: true });
            const currency = review.getByLabel(label("币种", "Currency"), { exact: true });
            const date = review.getByLabel(label("日期", "Date"), { exact: true });
            const preview = review.getByRole("region", {
                name: locale === "zh" ? "原件预览" : "Original preview",
                exact: true,
            });
            await expect(preview.getByRole("status")).toBeVisible();
            await amount.check();
            await date.scrollIntoViewIfNeeded();
            const originalTop = await date.evaluate(
                (element) => element.getBoundingClientRect().top + window.scrollY,
            );
            const box = await date.boundingBox();
            expect(box).not.toBeNull();
            await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
            await page.mouse.down();
            try {
                release();
                if (outcome === "ready") {
                    await expect(
                        preview.locator('canvas[data-preview-ready="true"]'),
                    ).toBeVisible();
                } else {
                    await expect(preview.getByRole("alert")).toBeVisible();
                }
                const loadedTop = await date.evaluate(
                    (element) => element.getBoundingClientRect().top + window.scrollY,
                );
                expect(Math.abs(loadedTop - originalTop)).toBeLessThan(1);
            } finally {
                release();
                await page.mouse.up();
            }
            await expect(date).toBeChecked();
            await expect(amount).toBeChecked();
            await expect(currency).not.toBeChecked();
            await preview.focus();
            await expect(preview).toBeFocused();
            await expectNoOverflow(page);
            expect(await api(page, "GET", `/api/v1/ledgers/${fixture.ledger}/operations`)).toEqual(
                [],
            );
            await page.screenshot({
                path: info.outputPath(`preview-stable-${outcome}-${locale}-${width}.png`),
                fullPage: true,
            });
        });
    }
}
