import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account, Balance } from "../src/ledger-api";
import type { BusinessDraft } from "../src/business-draft-api";
import type {
    BusinessPaymentReceipt,
    BusinessPaymentDetail,
    DocumentSettlementDetail,
} from "../src/business-settlement-api";

async function setup(page: Page, cross = false) {
    const data = await confirmationSetup(page, cross ? "bill" : "invoice");
    const sources: BusinessDraft[] = [];
    for (const price of cross ? ["100.00"] : ["160.00", "140.00"]) {
        const source = await api<BusinessDraft>(page, "POST", `${data.ledger}/business-documents`, {
            id: randomUUID(),
            document_kind: cross ? "bill" : "invoice",
            party_id: data.party.id,
            asset_id: cross ? "EUR" : "USD",
            issue_date: "2026-09-30",
            lines: [
                {
                    id: randomUUID(),
                    description: `Fictional later source ${price} 中文`,
                    quantity: "1.00",
                    unit_price: price,
                    tax_rate_percent: "0.00",
                    category_id: data.source.lines[0].category_id,
                    recognition_date: "2026-09-30",
                },
            ],
        });
        await api(page, "POST", `${data.ledger}/business-documents/${source.id}/confirm`, {
            id: randomUUID(),
            expected_version: 1,
        });
        sources.push(source);
    }
    const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
        name: "Fictional later allocation bank 中文",
        kind: "bank",
        asset_ids: ["USD"],
    });
    await api(page, "POST", `${data.ledger}/opening-balances`, {
        account_id: account.id,
        asset_id: "USD",
        amount: "1000.00",
        transaction_date: "2026-01-01",
        description: "Fictional later allocation opening",
    });
    const payments: BusinessPaymentReceipt[] = [];
    for (const amount of cross ? ["110.00"] : ["200.00", "100.00"])
        payments.push(
            await api<BusinessPaymentReceipt>(page, "POST", `${data.ledger}/business-payments`, {
                id: randomUUID(),
                direction: cross ? "payout" : "receipt",
                party_id: data.party.id,
                account_id: account.id,
                asset_id: "USD",
                amount,
                transaction_date: "2026-09-15",
                description: `Fictional existing payment ${amount}`,
                ...(cross
                    ? {
                          initial_allocation: {
                              id: randomUUID(),
                              allocation_date: "2026-09-30",
                              lines: [
                                  {
                                      document_id: sources[0].id,
                                      document_line_id: sources[0].lines[0].id,
                                      payment_amount: "44.00",
                                      document_amount: "40.00",
                                  },
                              ],
                          },
                      }
                    : {}),
            }),
        );
    await page.goto(`/?entity=${data.entity.id}&view=business-settlements`);
    return { ...data, sources, account, payments };
}

async function begin(page: Page, amount: string) {
    await page
        .locator(".master-records > li")
        .filter({ hasText: `Fictional existing payment ${amount}` })
        .getByRole("button", { name: "View details", exact: true })
        .click();
    await page.getByRole("button", { name: "Apply unapplied payment", exact: true }).click();
    await page.getByLabel("Allocation date", { exact: true }).fill("2026-09-30");
}
async function choose(page: Page, gross: string) {
    await page
        .locator(".master-records > li")
        .filter({ hasText: gross })
        .getByRole("button", { name: "Choose document lines", exact: true })
        .click();
    await page.getByRole("button", { name: "Select this line", exact: true }).click();
    await page.getByRole("button", { name: "Back to document list", exact: true }).click();
}
async function review(page: Page) {
    await page
        .getByRole("checkbox", { name: "I reviewed the original payment", exact: false })
        .check();
}

test("fictional two payments settle two documents with exact many-to-many allocations and no extra cash movement", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await setup(page);
    let writes = 0,
        cashWrites = 0;
    page.on("request", (request) => {
        if (request.method() === "POST" && request.url().endsWith("/business-allocations"))
            writes++;
        if (request.method() === "POST" && request.url().endsWith("/business-payments"))
            cashWrites++;
    });
    for (const [payment, quantities] of [
        ["200.00", ["100.00", "100.00"]],
        ["100.00", ["60.00", "40.00"]],
    ] as const) {
        await begin(page, payment);
        for (const [index, gross] of ["160.00 USD", "140.00 USD"].entries()) {
            await choose(page, gross);
            await page
                .getByLabel("Allocation amount (USD)", { exact: true })
                .nth(index)
                .fill(quantities[index]);
        }
        await review(page);
        if (payment === "200.00") {
            await page.setViewportSize({ width: 320, height: 1000 });
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath("later-many-to-many-en-320.png"),
                fullPage: true,
            });
        }
        await page
            .getByRole("button", { name: "Apply payment", exact: true })
            .evaluate((button) => {
                (button as HTMLButtonElement).click();
                (button as HTMLButtonElement).click();
            });
        await expect(
            page.getByText("Saved; the complete original receipt is verified.", { exact: true }),
        ).toBeVisible();
        await page.getByRole("button", { name: "Back to settlement list", exact: true }).click();
    }
    expect(writes).toBe(2);
    expect(cashWrites).toBe(0);
    await page
        .locator(".master-records > li")
        .filter({ hasText: "Fictional existing payment 200.00" })
        .getByRole("button", { name: "View details", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Apply unapplied payment", exact: true }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Back to settlement list", exact: true }).click();
    for (const payment of data.payments) {
        const saved = await api<BusinessPaymentDetail>(
            page,
            "GET",
            `${data.ledger}/business-payments/${payment.id}`,
        );
        expect(saved.unapplied_amount).toBe("0.00");
    }
    for (const source of data.sources) {
        const balance = await api<DocumentSettlementDetail>(
            page,
            "GET",
            `${data.ledger}/business-documents/${source.id}/settlement`,
        );
        expect(balance.outstanding_amount).toBe("0.00");
    }
    const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
    expect(
        balances.find((row) => row.account_id === data.account.id && row.asset_id === "USD")
            ?.amount,
    ).toBe("1300.00");
});

test("fictional cross-asset remainder after archived references replays lost success across closing and real 401", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await setup(page, true);
    await api(page, "POST", `${data.ledger}/period-changes`, {
        action: "close",
        expected_version: 1,
        closed_through: "2026-09-30",
        reason: "Fictional original document period closing",
    });
    await api(page, "PATCH", `${data.ledger}/business-parties/${data.party.id}`, {
        expected_version: 1,
        archived: true,
    });
    await api(page, "PATCH", `${data.ledger}/accounts/${data.account.id}`, {
        expected_version: 1,
        archived: true,
    });
    await page.reload();
    await begin(page, "110.00");
    await page.getByLabel("Allocation date", { exact: true }).fill("2026-10-05");
    await expect(page.getByText(data.payments[0].id, { exact: true })).toBeVisible();
    await expect(
        page.getByText("Fictional existing payment 110.00", { exact: true }),
    ).toBeVisible();
    await choose(page, "100.00 EUR");
    await page.getByLabel("Payment quantity (USD)", { exact: true }).fill("66.00");
    await page.getByLabel("Document quantity (EUR)", { exact: true }).fill("60.00");
    await review(page);
    const bodies: string[] = [],
        keys: string[] = [];
    await page.route(`**${data.ledger}/business-allocations`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        bodies.push(route.request().postData()!);
        keys.push(route.request().headers()["idempotency-key"]);
        if (bodies.length === 1) {
            expect((await route.fetch()).status()).toBe(201);
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
    await page.getByRole("button", { name: "Apply payment", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original request", exact: true }),
    ).toBeVisible();
    await expect(page.getByText(data.payments[0].id, { exact: true })).toBeVisible();
    await api(page, "POST", `${data.ledger}/period-changes`, {
        action: "close",
        expected_version: 2,
        closed_through: "2026-10-31",
        reason: "Fictional closing after completed allocation",
    });
    await page.setViewportSize({ width: 375, height: 1000 });
    await page.getByRole("button", { name: "中文", exact: true }).click();
    await expectNoOverflow(page);
    await page.screenshot({ path: info.outputPath("later-unknown-zh-375.png"), fullPage: true });
    await page.getByRole("button", { name: "English", exact: true }).click();
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(page.locator(".business-settlement-workspace")).toHaveCount(0);
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original request", exact: true }),
    ).toBeVisible();
    expect(bodies).toHaveLength(2);
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(
        page.getByText("Saved; the complete original receipt is verified.", { exact: true }),
    ).toBeVisible();
    expect(bodies).toHaveLength(3);
    expect(new Set(bodies).size).toBe(1);
    expect(new Set(keys).size).toBe(1);
    const payment = await api<BusinessPaymentDetail>(
        page,
        "GET",
        `${data.ledger}/business-payments/${data.payments[0].id}`,
    );
    expect(payment.unapplied_amount).toBe("0.00");
    const source = await api<DocumentSettlementDetail>(
        page,
        "GET",
        `${data.ledger}/business-documents/${data.sources[0].id}/settlement`,
    );
    expect(source.outstanding_amount).toBe("0.00");
    const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
    expect(
        balances.find((row) => row.account_id === data.account.id && row.asset_id === "USD")
            ?.amount,
    ).toBe("890.00");
});
