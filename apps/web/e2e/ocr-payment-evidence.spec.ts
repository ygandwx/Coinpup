import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, login, username } from "./helpers";
import { seed } from "./ocr-helpers";
import { parseAmount } from "../src/money";
import type { ReviewView } from "../src/ocr-api";
import type { Account, Category } from "../src/ledger-api";
import type { Party } from "../src/business-api";
import type { BusinessDraft } from "../src/business-draft-api";
import type {
    BusinessPaymentReceipt,
    DocumentSettlementDetail,
} from "../src/business-settlement-api";

async function paymentSetup(page: Page) {
    const fixture = seed(false, undefined, 3),
        base = `/api/v1/ledgers/${fixture.ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: "Fictional evidence bank 中文",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const category = await api<Category>(page, "POST", `${base}/categories`, {
        name: "Fictional evidence office 中文",
        kind: "expense",
    });
    const party = await api<Party>(page, "POST", `${base}/business-parties`, {
        id: randomUUID(),
        name: "Fictional evidence supplier 中文",
        role: "supplier",
    });
    const document = await api<BusinessDraft>(page, "POST", `${base}/business-documents`, {
        id: randomUUID(),
        document_kind: "bill",
        party_id: party.id,
        asset_id: "USD",
        issue_date: "2031-07-18",
        notes: "Fictional browser evidence; no real invoice",
        lines: [
            {
                id: randomUUID(),
                description: "Fictional settled service 中文",
                quantity: "1",
                unit_price: "10.00",
                tax_rate_percent: "0",
                category_id: category.id,
                recognition_date: "2031-07-18",
            },
        ],
    });
    await api(page, "POST", `${base}/business-documents/${document.id}/confirm`, {
        id: randomUUID(),
        expected_version: 1,
    });
    const payment = await api<BusinessPaymentReceipt>(page, "POST", `${base}/business-payments`, {
        id: randomUUID(),
        direction: "payout",
        party_id: party.id,
        account_id: account.id,
        asset_id: "USD",
        amount: "10.00",
        transaction_date: "2031-07-18",
        fees: [],
        initial_allocation: {
            id: randomUUID(),
            allocation_date: "2031-07-18",
            lines: [
                {
                    document_id: document.id,
                    document_line_id: document.lines[0].id,
                    payment_amount: "10.00",
                    document_amount: "10.00",
                },
            ],
        },
    });
    const paths = [
        `${base}/balances?limit=100`,
        `${base}/business-documents/${document.id}/settlement`,
        `${base}/business-payments/${payment.id}`,
    ];
    const before = await Promise.all(paths.map((path) => api(page, "GET", path)));
    return { fixture, base, payment, paths, before, account, party };
}

test("fictional bank row attaches to a settled payment through loss and real login recovery", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const { fixture, base, payment, paths, before } = await paymentSetup(page);
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await page.getByTestId(`ocr-draft-${fixture.drafts[0]}`).click();
    for (const field of ["Amount", "Currency", "Date"])
        await page.getByLabel(`${field} · Reviewed`, { exact: true }).check();
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("link_payment");
    await page.getByRole("button", { name: "Prepare entry", exact: true }).click();
    const chooser = page.getByLabel("Existing business payment", { exact: true });
    await chooser.selectOption(payment.id);
    const reviewLabel =
        "I reviewed the original payment and all initial allocations; this source row belongs to it";
    await expect(
        page.getByRole("button", { name: "Save payment link draft", exact: true }),
    ).toBeDisabled();
    await page.getByRole("checkbox", { name: reviewLabel, exact: true }).check();
    await page.getByRole("button", { name: "Save payment link draft", exact: true }).click();
    await expect(page.getByText(/Saved payment link:/u)).toBeVisible();
    const confirm = page.getByRole("button", {
        name: "Confirm payment link",
        exact: true,
    });
    await expect(confirm).toBeDisabled();
    await page.getByRole("checkbox", { name: reviewLabel, exact: true }).check();
    await expect(confirm).toBeEnabled();
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", {
                    name: language === "zh" ? "中文" : "English",
                    exact: true,
                })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`payment-evidence-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    const requests: string[] = [];
    await page.route("**/ocr-drafts/*/confirmations", async (route) => {
        requests.push(route.request().postData()!);
        if (requests.length === 1) {
            const response = await route.fetch();
            expect(response.status(), await response.text()).toBe(201);
            await route.abort("failed");
        } else if (requests.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            const revoked = await page.request.post("/api/v1/auth/logout", {
                headers: {
                    Origin: new URL(page.url()).origin,
                    "X-CSRF-Token": auth.csrf_token,
                },
            });
            expect(revoked.status()).toBe(204);
            await route.continue();
        } else await route.continue();
    });
    await confirm.dblclick();
    await expect(page.getByText(/Outcome unknown/u)).toBeVisible();
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
        page.getByRole("button", {
            name: "Retry original confirmation",
            exact: true,
        }),
    ).toBeVisible();
    expect(requests).toHaveLength(2);
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect(page.getByText(/Document confirmation completed/u)).toBeVisible();
    expect(requests).toHaveLength(3);
    expect(new Set(requests).size).toBe(1);
    expect(JSON.parse(requests[0]).entry).toEqual({
        kind: "link_payment",
        payment_id: payment.id,
        expected_version: 1,
    });
    expect(JSON.parse(requests[0])).not.toHaveProperty("originalPayment");
    expect(await Promise.all(paths.map((path) => api(page, "GET", path)))).toEqual(before);
    const settlement = await api<DocumentSettlementDetail>(page, "GET", paths[1]);
    expect(parseAmount(settlement.outstanding_amount, 2)).toBe(0n);
    const history = await api<{
        action: string;
        payment: BusinessPaymentReceipt;
    }>(page, "GET", `${base}/ocr-drafts/${fixture.drafts[0]}/confirmation`);
    expect(history.action).toBe("link_payment");
    expect(history.payment).toEqual(payment);
});

test("stale saved payment version requires a new explicit review without financial writes", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const data = await paymentSetup(page);
    const { fixture, base } = data;
    const payment = await api<BusinessPaymentReceipt>(page, "POST", `${base}/business-payments`, {
        id: randomUUID(),
        direction: "payout",
        party_id: data.party.id,
        account_id: data.account.id,
        asset_id: "USD",
        amount: "9007199254740993.01",
        transaction_date: "2031-07-18",
        fees: [],
    });
    const paths = [...data.paths, `${base}/business-payments/${payment.id}`];
    const before = await Promise.all(paths.map((path) => api(page, "GET", path)));
    const review = await api<ReviewView>(
        page,
        "GET",
        `${base}/ocr-drafts/${fixture.drafts[0]}/review`,
    );
    await api(page, "PUT", `${base}/ocr-drafts/${fixture.drafts[0]}/review`, {
        expected_version: review.version,
        review: {
            confirmed: review.fields.map((field) => field.path),
            entry: { kind: "link_payment", payment_id: payment.id, expected_version: 2 },
        },
    });
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await page.getByTestId(`ocr-draft-${fixture.drafts[0]}`).click();
    await expect(page.getByText(/9007199254740993\.01 USD/u)).toBeVisible();
    await expect(page.getByText(/The payment was cancelled or its version changed/u)).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Confirm payment link", exact: true }),
    ).toBeDisabled();
    for (const width of [375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`payment-evidence-stale-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    expect(await Promise.all(paths.map((path) => api(page, "GET", path)))).toEqual(before);
    expect(
        (await page.request.get(`${base}/ocr-drafts/${fixture.drafts[0]}/confirmation`)).status(),
    ).toBe(404);
});
