import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessConfirmationDetail } from "../src/business-confirmation-api";

test("fictional zero total stays a draft with an explicit confirmation explanation", async ({
    page,
}) => {
    const data = await confirmationSetup(page, "bill", "0.00");
    await page.getByRole("button", { name: "Review saved version", exact: true }).click();
    await expect(
        page.getByText(
            "Confirmation requires an active draft, an enabled asset and a positive gross total.",
            { exact: true },
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Confirm and post", exact: true }),
    ).toBeDisabled();
    expect(await api(page, "GET", `${data.ledger}/business-confirmations`)).toEqual([]);
});

for (const kind of ["invoice", "bill"] as const) {
    test(`fictional ${kind} confirmation reviews saved gross/tax and reads sealed zero lines`, async ({
        page,
    }, info) => {
        test.setTimeout(90_000);
        const data = await confirmationSetup(page, kind);
        await api(page, "PATCH", `${data.ledger}/business-parties/${data.party.id}`, {
            expected_version: 1,
            name: "Fictional renamed party",
        });
        await page.getByRole("button", { name: "Review saved version", exact: true }).click();
        await expect(
            page.getByRole("button", { name: "Confirm and post", exact: true }),
        ).toBeDisabled();
        await expect(page.locator(".confirmation-facts")).toContainText(
            "Fictional saved party 中文",
        );
        await expect(page.getByTestId("confirmation-totals")).toHaveText(
            "100.00 USD / 10.00 USD / 110.00 USD",
        );
        await expect(page.locator(".confirmation-lines > li")).toHaveCount(2);
        await expect(
            page.getByRole("button", { name: "Transactions", exact: true }),
        ).toBeDisabled();
        await page.getByRole("checkbox", { name: "I reviewed amounts", exact: false }).focus();
        await page.keyboard.press("Space");
        if (kind === "invoice") {
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
                        page.getByRole("checkbox", {
                            name: locale === "zh" ? "已核对金额" : "I reviewed amounts",
                            exact: false,
                        }),
                    ).toBeChecked();
                    await expectNoOverflow(page);
                    await page.screenshot({
                        path: info.outputPath(`confirmation-review-${locale}-${width}.png`),
                        fullPage: true,
                    });
                }
        }
        let posts = 0;
        await page.route(`**${data.base}/confirm`, async (route) => {
            posts += 1;
            await route.continue();
        });
        await page
            .getByRole("button", { name: "Confirm and post", exact: true })
            .evaluate((button) => {
                (button as HTMLButtonElement).click();
                (button as HTMLButtonElement).click();
            });
        await expect(
            page.getByText("Confirmed; original amounts and journal mappings are sealed.", {
                exact: true,
            }),
        ).toBeVisible();
        expect(posts).toBe(1);
        const detail = await api<BusinessConfirmationDetail>(
            page,
            "GET",
            `${data.base}/confirmation`,
        );
        expect(detail.confirmation.postings).toHaveLength(1);
        expect(detail.document).toMatchObject({
            state: "confirmed",
            net_amount: "100.00",
            tax_amount: "10.00",
            total_amount: "110.00",
            version: 2,
        });
        await page.getByRole("button", { name: "Back to documents", exact: true }).click();
        await page.getByRole("button", { name: "View sealed document", exact: true }).click();
        await expect(page.locator(".confirmation-lines")).toContainText(
            detail.confirmation.postings[0].journal_id,
        );
        await expect(page.locator(".confirmation-lines")).toContainText("Zero amount; no posting");
        await expect(
            page.getByRole("button", { name: "Confirm and post", exact: true }),
        ).toHaveCount(0);
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`confirmation-sealed-${kind}.png`),
            fullPage: true,
        });
        await page.getByRole("button", { name: "Back to documents", exact: true }).click();
        await page.getByRole("button", { name: "Drafts to confirm", exact: true }).click();
        await expect(page.getByText("No documents on this page.", { exact: true })).toBeVisible();
    });
}

test("fictional long amount stays exact on mobile and a closed period rejects confirmation", async ({
    page,
}, info) => {
    const data = await confirmationSetup(page, "invoice", "999999999999999999.99");
    await page.setViewportSize({ width: 320, height: 1000 });
    await page.getByRole("button", { name: "Review saved version", exact: true }).click();
    await expect(page.getByTestId("confirmation-totals")).toContainText(
        "999,999,999,999,999,999.99 USD",
    );
    await expectNoOverflow(page);
    await page.screenshot({ path: info.outputPath("confirmation-long-320.png"), fullPage: true });
    await api(page, "POST", `${data.ledger}/period-changes`, {
        action: "close",
        expected_version: 1,
        closed_through: "2026-01-02",
        reason: "Fictional closed confirmation period",
    });
    await page.getByRole("checkbox", { name: "I reviewed amounts", exact: false }).check();
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    await expect(page.locator(".business-confirmation-panel [role=alert]")).toContainText(
        "The transaction or recognition date is closed",
    );
    await expect(
        page.getByRole("button", { name: "Back to documents", exact: true }),
    ).toBeEnabled();
    expect((await api<{ version: number }>(page, "GET", data.base)).version).toBe(1);
    expect(await api(page, "GET", `${data.ledger}/business-confirmations`)).toEqual([]);
});
