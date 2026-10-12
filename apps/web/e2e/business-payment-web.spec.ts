import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account, Category, Balance } from "../src/ledger-api";
import type { BusinessPaymentDetail, BusinessPaymentSummary } from "../src/business-settlement-api";

async function fillPayment(page: Page, direction: "receipt" | "payout" = "receipt") {
    const data = await confirmationSetup(page);
    const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
        name: "Fictional payment bank 中文",
        kind: "bank",
        asset_ids: ["USD"],
    });
    await api(page, "POST", `${data.ledger}/opening-balances`, {
        account_id: account.id,
        asset_id: "USD",
        amount: "1000.00",
        transaction_date: "2026-01-01",
        description: "Fictional payment opening",
    });
    const categories = await api<Category[]>(page, "GET", `${data.ledger}/categories?limit=100`);
    const category = categories.find((row) => row.kind === "expense")!;
    await page.goto(`/?entity=${data.entity.id}&view=business-settlements`);
    await page.getByRole("button", { name: "New payment", exact: true }).click();
    await page.getByLabel("Payment direction", { exact: true }).selectOption(direction);
    await page.getByLabel("Business party", { exact: true }).selectOption(data.party.id);
    await page.getByLabel("Money account", { exact: true }).selectOption(account.id);
    await page.getByLabel("Principal asset", { exact: true }).selectOption("USD");
    await page.getByLabel("Principal", { exact: true }).fill("10.00");
    await page.getByLabel("Transaction date", { exact: true }).fill("2026-09-15");
    await page.getByLabel("Description", { exact: true }).fill("Fictional payment only 中文");
    await page.getByRole("button", { name: "Add fee", exact: true }).click();
    await page.getByLabel("Fee account", { exact: true }).selectOption(account.id);
    await page.getByLabel("Fee asset", { exact: true }).selectOption("USD");
    await page.getByLabel("Fee amount", { exact: true }).fill("1.00");
    await page.getByLabel("Fee category", { exact: true }).selectOption(category.id);
    await page.getByRole("checkbox", { name: "I reviewed direction", exact: false }).check();
    return { ...data, account, category };
}

for (const direction of ["receipt", "payout"] as const)
    test(`fictional ${direction} form preserves bilingual review and posts one principal plus fee`, async ({
        page,
    }, info) => {
        test.setTimeout(90_000);
        const data = await fillPayment(page, direction);
        if (direction === "receipt")
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
                            name: locale === "zh" ? "已核对收付方向" : "I reviewed direction",
                            exact: false,
                        }),
                    ).toBeChecked();
                    await expect(
                        page.getByLabel(locale === "zh" ? "本金" : "Principal", {
                            exact: true,
                        }),
                    ).toHaveValue("10.00");
                    await expectNoOverflow(page);
                    await page.screenshot({
                        path: info.outputPath(`payment-form-${locale}-${width}.png`),
                        fullPage: true,
                    });
                }
        let writes = 0;
        await page.route(`**${data.ledger}/business-payments`, async (route) => {
            if (route.request().method() !== "POST") return route.continue();
            writes++;
            await route.continue();
        });
        if (direction === "payout") {
            await page.getByLabel("Principal", { exact: true }).focus();
            await page.keyboard.press("Enter");
            await page.keyboard.press("Enter");
        } else {
            await page
                .getByRole("button", { name: "Record actual payment", exact: true })
                .evaluate((button) => {
                    (button as HTMLButtonElement).click();
                    (button as HTMLButtonElement).click();
                });
        }
        await expect(
            page.getByText("Saved; the complete original receipt is verified.", {
                exact: true,
            }),
        ).toBeVisible();
        expect(writes).toBe(1);
        const payments = await api<BusinessPaymentSummary[]>(
            page,
            "GET",
            `${data.ledger}/business-payments`,
        );
        expect(payments).toHaveLength(1);
        const detail = await api<BusinessPaymentDetail>(
            page,
            "GET",
            `${data.ledger}/business-payments/${payments[0].id}`,
        );
        expect(detail).toMatchObject({
            direction,
            amount: "10.00",
            unapplied_amount: "10.00",
            applied_amount: "0.00",
            fees: [{ amount: "1.00" }],
            receipt: { initial_allocation: null },
        });
        const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
        expect(
            balances.find((row) => row.account_id === data.account.id && row.asset_id === "USD")
                ?.amount,
        ).toBe(direction === "receipt" ? "1009.00" : "989.00");
        expect(await api(page, "GET", `${data.ledger}/business-confirmations`)).toEqual([]);
        await page.getByRole("button", { name: "Back to settlement list", exact: true }).click();
        await expect(page.locator(".business-settlement-panel")).toContainText(
            "Unapplied: 10.00 USD",
        );
    });

test("fictional payment replays the original intent after lost success, entity archive, closed period and real 401", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await fillPayment(page);
    const bodies: string[] = [],
        keys: string[] = [];
    await page.route(`**${data.ledger}/business-payments`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        bodies.push(route.request().postData()!);
        keys.push(route.request().headers()["idempotency-key"]);
        if (bodies.length === 1) {
            const result = await route.fetch();
            expect(result.status()).toBe(201);
            return route.abort("failed");
        }
        if (bodies.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            expect(
                (
                    await page.request.post("/api/v1/auth/logout", {
                        headers: {
                            Origin: new URL(page.url()).origin,
                            "X-CSRF-Token": auth.csrf_token,
                        },
                    })
                ).status(),
            ).toBe(204);
        }
        await route.continue();
    });
    await page.getByRole("button", { name: "Record actual payment", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original request", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Transactions", exact: true })).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Back to settlement list", exact: true }),
    ).toHaveCount(0);
    await api(page, "POST", `${data.ledger}/period-changes`, {
        action: "close",
        expected_version: 1,
        closed_through: "2026-09-30",
        reason: "Fictional period closed after successful payment",
    });
    await api(page, "PATCH", `/api/v1/entities/${data.entity.id}`, {
        expected_version: 1,
        archived: true,
    });
    await page.setViewportSize({ width: 320, height: 1000 });
    await expectNoOverflow(page);
    await page.screenshot({
        path: info.outputPath("payment-unknown-en-320.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(page.locator(".business-settlement-workspace")).toHaveCount(0);
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original request", exact: true }),
    ).toBeVisible();
    expect(bodies).toHaveLength(2);
    await page
        .getByRole("button", { name: "Retry original request", exact: true })
        .evaluate((button) => {
            (button as HTMLButtonElement).click();
            (button as HTMLButtonElement).click();
        });
    await expect(
        page.getByText("Saved; the complete original receipt is verified.", {
            exact: true,
        }),
    ).toBeVisible();
    expect(bodies).toHaveLength(3);
    expect(new Set(bodies).size).toBe(1);
    expect(new Set(keys).size).toBe(1);
    const payments = await api<BusinessPaymentSummary[]>(
        page,
        "GET",
        `${data.ledger}/business-payments`,
    );
    expect(payments).toHaveLength(1);
    expect(payments[0].id).toBe(JSON.parse(bodies[0]).id);
});
