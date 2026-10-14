import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, login } from "./helpers";
import { seed } from "./ocr-helpers";
import type { Account } from "../src/ledger-api";
import type { Party } from "../src/business-api";
import type { ReviewView } from "../src/ocr-api";
import type { BusinessPaymentReceipt } from "../src/business-settlement-api";

test("batch payment evidence requires every original review and pauses on an unknown middle row", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed(false, undefined, 3),
        base = `/api/v1/ledgers/${fixture.ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: "Fictional batch evidence bank 中文",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const party = await api<Party>(page, "POST", `${base}/business-parties`, {
        id: randomUUID(),
        name: "Fictional batch evidence party 中文",
        role: "supplier",
    });
    const payments: BusinessPaymentReceipt[] = [];
    for (const [index, amount] of ["10.00", "20.00", "30.00"].entries()) {
        const payment = await api<BusinessPaymentReceipt>(
            page,
            "POST",
            `${base}/business-payments`,
            {
                id: randomUUID(),
                direction: "payout",
                party_id: party.id,
                account_id: account.id,
                asset_id: "USD",
                amount,
                transaction_date: "2031-07-18",
                fees: [],
            },
        );
        payments.push(payment);
        const review = await api<ReviewView>(
            page,
            "GET",
            `${base}/ocr-drafts/${fixture.drafts[index]}/review`,
        );
        await api(page, "PUT", `${base}/ocr-drafts/${fixture.drafts[index]}/review`, {
            expected_version: review.version,
            review: {
                confirmed: review.fields.map((field) => field.path),
                entry: { kind: "link_payment", payment_id: payment.id, expected_version: 1 },
            },
        });
    }
    const paths = [
        `${base}/balances?limit=100`,
        ...payments.map((p) => `${base}/business-payments/${p.id}`),
    ];
    const before = await Promise.all(paths.map((path) => api(page, "GET", path)));
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    for (const id of fixture.drafts) await page.getByTestId(`select-draft-${id}`).check();
    await page.getByRole("button", { name: "Review selected drafts", exact: true }).click();
    const confirm = page.getByRole("button", { name: "Confirm selected drafts", exact: true });
    await expect(confirm).toBeDisabled();
    for (let i = 1; i <= 3; i++) {
        await page
            .getByRole("checkbox", {
                name: `Reviewed original payment and complete initial allocations for draft ${i}`,
                exact: true,
            })
            .check();
        if (i < 3) await expect(confirm).toBeDisabled();
    }
    await expect(confirm).toBeEnabled();
    await page.getByRole("button", { name: "Cancel batch review", exact: true }).click();
    await page.getByRole("button", { name: "Review selected drafts", exact: true }).click();
    await expect(confirm).toBeDisabled();
    for (let i = 1; i <= 3; i++) {
        const review = page.getByRole("checkbox", {
            name: `Reviewed original payment and complete initial allocations for draft ${i}`,
            exact: true,
        });
        await expect(review).not.toBeChecked();
        await review.check();
    }
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`payment-batch-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    const requests: string[] = [];
    await page.route("**/ocr-drafts/*/confirmations", async (route) => {
        requests.push(route.request().postData()!);
        if (requests.length === 2) {
            const result = await route.fetch();
            expect(result.status()).toBe(201);
            await route.abort("failed");
        } else await route.continue();
    });
    await confirm.click();
    await expect(
        page.getByRole("button", { name: "Retry original confirmation", exact: true }),
    ).toBeVisible();
    expect(requests).toHaveLength(2);
    expect(
        (await page.request.get(`${base}/ocr-drafts/${fixture.drafts[2]}/confirmation`)).status(),
    ).toBe(404);
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect(page.getByText(/Document confirmation completed/u)).toBeVisible();
    expect(requests).toHaveLength(4);
    expect(requests[1]).toBe(requests[2]);
    expect(await Promise.all(paths.map((path) => api(page, "GET", path)))).toEqual(before);
    for (const [index, payment] of payments.entries()) {
        const record = await api<{ action: string; payment: BusinessPaymentReceipt }>(
            page,
            "GET",
            `${base}/ocr-drafts/${fixture.drafts[index]}/confirmation`,
        );
        expect(record.action).toBe("link_payment");
        expect(record.payment).toEqual(payment);
    }
});
