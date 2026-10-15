import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account, Balance, Category } from "../src/ledger-api";
import type { BusinessPaymentReceipt, BusinessPaymentDetail } from "../src/business-settlement-api";
import type { AllocationReturnCapacity } from "../src/allocation-return-api";
import type { RefundSummary } from "../src/refund-api";
import { parseAmount } from "../src/money";

for (const [direction, asset, principal] of [
    ["receipt", "USD", "100"],
    ["payout", "EUR", "100"],
    ["receipt", "USD", "90000000000000000000"],
] as const)
    test(`fictional ${direction}/${asset}/${principal} refund preserves original intent`, async ({
        page,
    }, info) => {
        test.setTimeout(120_000);
        const data = await confirmationSetup(
            page,
            direction === "receipt" ? "invoice" : "bill",
            principal,
        );
        await api(page, "POST", `${data.base}/confirm`, { id: randomUUID(), expected_version: 1 });
        const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
            name: "Fictional actual refund bank",
            kind: "bank",
            asset_ids: ["USD", "EUR"],
        });
        const paid = await api<BusinessPaymentReceipt>(
            page,
            "POST",
            `${data.ledger}/business-payments`,
            {
                id: randomUUID(),
                direction,
                party_id: data.party.id,
                account_id: account.id,
                asset_id: "USD",
                amount: principal,
                transaction_date: "2026-01-02",
                description: "Fictional refund original 中文",
                initial_allocation: {
                    id: randomUUID(),
                    allocation_date: "2026-01-03",
                    lines: [
                        {
                            document_id: data.source.id,
                            document_line_id: data.source.lines[0].id,
                            payment_amount: principal,
                            document_amount: principal,
                        },
                    ],
                },
            },
        );
        const capacity = await api<AllocationReturnCapacity>(
            page,
            "GET",
            `${data.ledger}/business-allocations/${paid.initial_allocation!.id}/return-capacity`,
        );
        const returnId = randomUUID();
        await api(page, "POST", `${data.ledger}/business-allocation-returns`, {
            id: returnId,
            payment_id: paid.id,
            return_date: "2026-01-04",
            reason: "Fictional refund source return",
            lines: [
                {
                    source_allocation_line_id: capacity.lines[0].source_allocation_line_id,
                    expected_version: 1,
                    payment_amount: "30",
                    document_amount: "30",
                },
            ],
        });
        const categories = await api<Category[]>(
            page,
            "GET",
            `${data.ledger}/categories?limit=100`,
        );
        const expense = categories.find((row) => row.kind === "expense")!;
        const before = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
        const savedDocument = await api(page, "GET", `${data.base}/settlement`);
        await page.goto(`/?entity=${data.entity.id}&view=business-settlements`);
        async function openRefund() {
            await page.getByRole("button", { name: "View details", exact: true }).click();
            await page.getByRole("button", { name: "Refund this payment", exact: true }).click();
            await page.getByLabel("Money account", { exact: true }).selectOption(account.id);
            await page.getByLabel("Principal asset", { exact: true }).selectOption(asset);
            await page
                .getByLabel("Actual refund quantity", { exact: true })
                .fill(asset === "EUR" ? "19.37" : "20");
            await page
                .getByLabel("Original principal being refunded (USD)", { exact: true })
                .fill("20");
            await page.getByLabel("Refund transaction date", { exact: true }).fill("2026-01-05");
            await page
                .getByLabel("Refund reason", { exact: true })
                .fill("Fictional refund 中文 " + "reason ".repeat(12));
        }
        await openRefund();
        await page.getByRole("checkbox", { name: new RegExp(returnId, "u") }).check();
        await page.getByRole("button", { name: "Add fee", exact: true }).click();
        await page.getByLabel("Fee account", { exact: true }).selectOption(account.id);
        await page.getByLabel("Fee asset", { exact: true }).selectOption("USD");
        await page.getByLabel("Fee amount", { exact: true }).fill("0.25");
        await page.getByLabel("Fee category", { exact: true }).selectOption(expense.id);
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
                    page.getByLabel(locale === "zh" ? "退款原因" : "Refund reason", {
                        exact: true,
                    }),
                ).toHaveValue(/Fictional refund 中文/u);
                await expect(
                    page.getByLabel(locale === "zh" ? "实际退款数量" : "Actual refund quantity", {
                        exact: true,
                    }),
                ).toHaveValue(asset === "EUR" ? "19.37" : "20");
                await expectNoOverflow(page);
                await page.screenshot({
                    path: info.outputPath(`refund-${direction}-${asset}-${locale}-${width}.png`),
                    fullPage: true,
                });
            }
        await page
            .getByRole("checkbox", { name: "I reviewed original principal", exact: false })
            .check();
        const bodies: string[] = [],
            keys: string[] = [];
        await page.route(`**${data.ledger}/business-refunds`, async (route) => {
            if (route.request().method() !== "POST") return route.continue();
            bodies.push(route.request().postData()!);
            keys.push(route.request().headers()["idempotency-key"]);
            if (bodies.length === 1) {
                const response = await route.fetch();
                expect(response.status()).toBe(201);
                return route.abort("failed");
            }
            if (bodies.length === 2) {
                const session = await (await page.request.get("/api/v1/auth/session")).json();
                expect(
                    (
                        await page.request.post("/api/v1/auth/logout", {
                            headers: {
                                Origin: new URL(page.url()).origin,
                                "X-CSRF-Token": session.csrf_token,
                            },
                        })
                    ).status(),
                ).toBe(204);
            }
            await route.continue();
        });
        await page
            .getByRole("button", { name: "Record actual refund", exact: true })
            .evaluate((button) => {
                (button as HTMLButtonElement).click();
                (button as HTMLButtonElement).click();
            });
        await expect(
            page.getByRole("button", { name: "Retry original request", exact: true }),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Transactions", exact: true }),
        ).toBeDisabled();
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`refund-unknown-${direction}-${asset}-en-320.png`),
            fullPage: true,
        });
        await page.getByRole("button", { name: "Retry original request", exact: true }).click();
        await page.getByLabel("Username", { exact: true }).fill(username);
        await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
        await page.getByRole("button", { name: "Sign in", exact: true }).click();
        await page.getByRole("button", { name: "Retry original request", exact: true }).click();
        await expect(
            page.getByText("Saved; the complete original receipt is verified.", { exact: true }),
        ).toBeVisible();
        expect(bodies).toHaveLength(3);
        expect(new Set(bodies).size).toBe(1);
        expect(new Set(keys).size).toBe(1);
        expect(JSON.parse(bodies[0]).allocation_return_ids).toEqual([returnId]);
        const refunds = await api<RefundSummary[]>(page, "GET", `${data.ledger}/business-refunds`);
        expect(refunds).toHaveLength(1);
        expect(await api(page, "GET", `${data.base}/settlement`)).toEqual(savedDocument);
        const payment = await api<BusinessPaymentDetail>(
            page,
            "GET",
            `${data.ledger}/business-payments/${paid.id}`,
        );
        expect(payment.receipt).toEqual(paid);
        expect(payment.unapplied_amount).toBe("10.00");
        const after = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
        for (const cashAsset of ["USD", "EUR"]) {
            const quantity = (rows: Balance[]) =>
                parseAmount(
                    rows.find((row) => row.account_id === account.id && row.asset_id === cashAsset)
                        ?.amount ?? "0",
                    2,
                );
            const actual =
                asset === cashAsset
                    ? parseAmount(asset === "EUR" ? "19.37" : "20", 2) *
                      (direction === "receipt" ? -1n : 1n)
                    : 0n;
            expect(quantity(after) - quantity(before)).toBe(
                actual - (cashAsset === "USD" ? 25n : 0n),
            );
        }
        await page.getByRole("button", { name: "Back to settlement list", exact: true }).click();
        await page.reload();
        let failed = false;
        await page.route(`**${data.ledger}/business-refunds?*`, (route) => {
            if (!failed) {
                failed = true;
                return route.abort("failed");
            }
            return route.continue();
        });
        await page.getByRole("button", { name: "Actual refund history", exact: true }).click();
        const history = page.getByRole("region", { name: "Actual refund history", exact: true });
        await expect(history.getByRole("alert")).toBeVisible();
        await history.getByRole("button", { name: "Reload", exact: true }).click();
        await history.getByRole("button", { name: "View refund details", exact: true }).click();
        await expect(history).toContainText(returnId);
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`refund-history-${direction}-${asset}-en-320.png`),
            fullPage: true,
        });
        await history.getByRole("button", { name: "Back to settlement list", exact: true }).click();
        await api(page, "POST", `${data.ledger}/period-changes`, {
            action: "close",
            expected_version: 1,
            closed_through: "2026-01-05",
            reason: "Fictional refund rejection",
        });
        await page.unroute(`**${data.ledger}/business-refunds`);
        await openRefund();
        await page.getByLabel("Actual refund quantity", { exact: true }).fill("1");
        await page.getByLabel("Original principal being refunded (USD)", { exact: true }).fill("1");
        await page
            .getByRole("checkbox", { name: "I reviewed original principal", exact: false })
            .check();
        await page.getByRole("button", { name: "Record actual refund", exact: true }).click();
        await expect(page.getByRole("alert")).toContainText("closed");
        for (const [locale, width] of [
            ["en", 375],
            ["zh", 320],
        ] as const) {
            await page.setViewportSize({ width, height: 1000 });
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`refund-period-${direction}-${asset}-${locale}-${width}.png`),
                fullPage: true,
            });
        }
        await page.getByRole("button", { name: "English", exact: true }).click();
        await page.getByRole("button", { name: "Edit rejected request", exact: true }).click();
        await expect(page.getByLabel("Actual refund quantity", { exact: true })).toHaveValue("1");
        await expect(page.getByLabel("Refund transaction date", { exact: true })).toHaveValue(
            "2026-01-05",
        );
        expect(await api(page, "GET", `${data.ledger}/business-refunds`)).toEqual(refunds);
    });
