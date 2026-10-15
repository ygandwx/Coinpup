import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account } from "../src/ledger-api";
import type { BusinessDraft } from "../src/business-draft-api";
import type { BusinessPaymentReceipt, BusinessPaymentDetail } from "../src/business-settlement-api";
import type { AllocationReturnSummary } from "../src/allocation-return-api";

for (const [asset, principal] of [
    ["USD", "100"],
    ["EUR", "100"],
    ["USD", "90000000000000000000"],
] as const)
    test(`fictional ${asset}/${principal} return preserves cash and original intent`, async ({
        page,
    }, info) => {
        test.setTimeout(120_000);
        const data = await confirmationSetup(page, "invoice", principal);
        const source =
            asset === "USD"
                ? data.source
                : await api<BusinessDraft>(page, "POST", `${data.ledger}/business-documents`, {
                      id: randomUUID(),
                      document_kind: "invoice",
                      party_id: data.party.id,
                      asset_id: asset,
                      issue_date: "2026-01-02",
                      lines: data.source.lines.map((line) => ({
                          id: randomUUID(),
                          description: line.description,
                          quantity: line.quantity,
                          unit_price: line.unit_price,
                          tax_rate_percent: line.tax_rate_percent,
                          category_id: line.category_id,
                          recognition_date: line.recognition_date,
                      })),
                  });
        await api(page, "POST", `${data.ledger}/business-documents/${source.id}/confirm`, {
            id: randomUUID(),
            expected_version: 1,
        });
        const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
            name: "Fictional return bank",
            kind: "bank",
            asset_ids: ["USD"],
        });
        const paid = await api<BusinessPaymentReceipt>(
            page,
            "POST",
            `${data.ledger}/business-payments`,
            {
                id: randomUUID(),
                direction: "receipt",
                party_id: data.party.id,
                account_id: account.id,
                asset_id: "USD",
                amount: asset === "EUR" ? "110" : principal,
                transaction_date: "2026-01-02",
                description: "Fictional return source",
                initial_allocation: {
                    id: randomUUID(),
                    allocation_date: "2026-01-03",
                    lines: [
                        {
                            document_id: source.id,
                            document_line_id: source.lines[0].id,
                            payment_amount: asset === "EUR" ? "110" : principal,
                            document_amount: principal,
                        },
                    ],
                },
            },
        );
        const balances = await api(page, "GET", `${data.ledger}/balances?limit=100`);
        await page.goto(`/?entity=${data.entity.id}&view=business-settlements`);
        async function openReturn() {
            await page.getByRole("button", { name: "All allocations", exact: true }).click();
            await page.getByRole("button", { name: "View details", exact: true }).click();
            await page.getByRole("button", { name: "Return this allocation", exact: true }).click();
            await page
                .getByRole("checkbox", { name: source.lines[0].description, exact: true })
                .check();
            await page.getByLabel("Return effective date", { exact: true }).fill("2026-01-04");
            await page
                .getByLabel("Return reason", { exact: true })
                .fill("Fictional return 中文 " + "reason ".repeat(12));
            await page.getByLabel("Document amount to return", { exact: true }).fill("20");
        }
        await openReturn();
        for (const width of [1440, 375, 320])
            for (const locale of ["zh", "en"]) {
                await page.setViewportSize({ width, height: 1000 });
                await page
                    .getByRole("button", {
                        name: locale === "zh" ? "中文" : "English",
                        exact: true,
                    })
                    .click();
                await expect(page.getByRole("form")).toContainText(
                    asset === "EUR" ? "22.00 USD" : "20.00 USD",
                );
                await expect(page.locator('input[inputmode="decimal"]')).toHaveValue("20");
                await expectNoOverflow(page);
                await page.screenshot({
                    path: info.outputPath(`return-${asset}-${locale}-${width}.png`),
                    fullPage: true,
                });
            }
        await page
            .getByRole("checkbox", { name: "I reviewed the original sources", exact: false })
            .check();
        const bodies: string[] = [],
            keys: string[] = [];
        await page.route(`**${data.ledger}/business-allocation-returns`, async (route) => {
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
            .getByRole("button", { name: "Confirm allocation return", exact: true })
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
            path: info.outputPath(`return-unknown-${asset}-en-320.png`),
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
        expect(
            await api<AllocationReturnSummary[]>(
                page,
                "GET",
                `${data.ledger}/business-allocation-returns`,
            ),
        ).toHaveLength(1);
        expect(await api(page, "GET", `${data.ledger}/balances?limit=100`)).toEqual(balances);
        const payment = await api<BusinessPaymentDetail>(
            page,
            "GET",
            `${data.ledger}/business-payments/${paid.id}`,
        );
        expect(payment.receipt).toEqual(paid);
        expect(payment.unapplied_amount).toBe(asset === "EUR" ? "22.00" : "20.00");
        await page.getByRole("button", { name: "Back to settlement list", exact: true }).click();
        await page.getByRole("button", { name: "Allocation return history", exact: true }).click();
        await page.getByRole("button", { name: "View return details", exact: true }).click();
        await expect(page.getByRole("region", { name: "Allocation return history" })).toContainText(
            `20.00 ${asset}`,
        );
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`return-history-${asset}-en-320.png`),
            fullPage: true,
        });
        await page.getByRole("button", { name: "Back to settlement list", exact: true }).click();
        await api(page, "POST", `${data.ledger}/period-changes`, {
            action: "close",
            expected_version: 1,
            closed_through: "2026-01-04",
            reason: "Fictional return rejection",
        });
        await page.unroute(`**${data.ledger}/business-allocation-returns`);
        await openReturn();
        await page
            .getByRole("checkbox", { name: "I reviewed the original sources", exact: false })
            .check();
        await page.getByRole("button", { name: "Confirm allocation return", exact: true }).click();
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
                path: info.outputPath(`return-period-${asset}-${locale}-${width}.png`),
                fullPage: true,
            });
        }
        await page.getByRole("button", { name: "English", exact: true }).click();
        await page.getByRole("button", { name: "Edit rejected request", exact: true }).click();
        await expect(page.getByLabel("Document amount to return", { exact: true })).toHaveValue(
            "20",
        );
        await expect(page.getByLabel("Return effective date", { exact: true })).toHaveValue(
            "2026-01-04",
        );
        expect(
            await api<AllocationReturnSummary[]>(
                page,
                "GET",
                `${data.ledger}/business-allocation-returns`,
            ),
        ).toHaveLength(1);
    });
