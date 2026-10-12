import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Account, Balance, Entity, Category } from "../src/ledger-api";
import type { BusinessPaymentSummary } from "../src/business-settlement-api";

async function setup(page: Page, direction: "receipt" | "payout") {
    const data = await confirmationSetup(page);
    const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
        name: "Fictional payment edge bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    await page.goto(`/?entity=${data.entity.id}&view=business-settlements`);
    await page.getByRole("button", { name: "New payment", exact: true }).click();
    await page.getByLabel("Payment direction", { exact: true }).selectOption(direction);
    await page.getByLabel("Business party", { exact: true }).selectOption(data.party.id);
    await page.getByLabel("Money account", { exact: true }).selectOption(account.id);
    await page.getByLabel("Principal asset", { exact: true }).selectOption("USD");
    await page.getByLabel("Principal", { exact: true }).fill("100.00");
    await page.getByLabel("Transaction date", { exact: true }).fill("2026-09-15");
    await page.getByLabel("Description", { exact: true }).fill("Fictional private payment edge");
    return { ...data, account };
}
async function submit(page: Page) {
    await page.getByRole("checkbox", { name: "I reviewed direction", exact: false }).check();
    await page.getByRole("button", { name: "Record actual payment", exact: true }).click();
}
async function signIn(page: Page) {
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

test("fictional payment rejects final balance overflow and closed dates while retaining legal negative balances", async ({
    page,
}, info) => {
    const data = await setup(page, "payout");
    const categories = await api<Category[]>(page, "GET", `${data.ledger}/categories?limit=100`);
    await page.getByLabel("Principal", { exact: true }).fill("99999999999999999999.99");
    await page.getByRole("button", { name: "Add fee", exact: true }).click();
    await page.getByLabel("Fee account", { exact: true }).selectOption(data.account.id);
    await page.getByLabel("Fee asset", { exact: true }).selectOption("USD");
    await page.getByLabel("Fee amount", { exact: true }).fill("1.00");
    await page
        .getByLabel("Fee category", { exact: true })
        .selectOption(categories.find((row) => row.kind === "expense")!.id);
    const bodies: { id: string; transaction_date: string; amount: string }[] = [],
        keys: string[] = [];
    await page.route(`**${data.ledger}/business-payments`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        bodies.push(route.request().postDataJSON());
        keys.push(route.request().headers()["idempotency-key"]);
        await route.continue();
    });
    await submit(page);
    await expect(
        page.getByRole("button", { name: "Edit rejected request", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("final account balance exceeds");
    await page.getByRole("button", { name: "中文", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("最终账户余额超出支持范围");
    await page.setViewportSize({ width: 320, height: 1000 });
    await expectNoOverflow(page);
    await page.screenshot({
        path: info.outputPath("payment-range-rejection-zh-320.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "English", exact: true }).click();
    expect(await api(page, "GET", `${data.ledger}/business-payments`)).toEqual([]);
    await api(page, "POST", `${data.ledger}/period-changes`, {
        action: "close",
        expected_version: 1,
        closed_through: "2026-09-30",
        reason: "Fictional period boundary",
    });
    await page.getByRole("button", { name: "Edit rejected request", exact: true }).click();
    await expect(page.getByLabel("Principal", { exact: true })).toHaveValue(
        "99999999999999999999.99",
    );
    await page.getByLabel("Principal", { exact: true }).fill("100.00");
    await expect(page.getByLabel("Transaction date", { exact: true })).toHaveValue("2026-09-15");
    await expect(
        page.getByRole("checkbox", { name: "I reviewed direction", exact: false }),
    ).not.toBeChecked();
    await submit(page);
    await expect(
        page.getByRole("button", { name: "Edit rejected request", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("payment or allocation date is closed");
    await page.setViewportSize({ width: 375, height: 1000 });
    await expectNoOverflow(page);
    await page.screenshot({
        path: info.outputPath("payment-period-rejection-en-375.png"),
        fullPage: true,
    });
    expect(await api(page, "GET", `${data.ledger}/business-payments`)).toEqual([]);
    await page.getByRole("button", { name: "Edit rejected request", exact: true }).click();
    await page.getByLabel("Transaction date", { exact: true }).fill("2026-10-01");
    await submit(page);
    await expect(
        page.getByText("Saved; the complete original receipt is verified.", { exact: true }),
    ).toBeVisible();
    expect(bodies).toHaveLength(3);
    expect(new Set(keys).size).toBe(3);
    expect(new Set(bodies.map((body) => body.id)).size).toBe(1);
    expect(bodies.map((body) => body.transaction_date)).toEqual([
        "2026-09-15",
        "2026-09-15",
        "2026-10-01",
    ]);
    expect(await api(page, "GET", `${data.ledger}/business-payments`)).toHaveLength(1);
    const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
    expect(
        balances.find((row) => row.account_id === data.account.id && row.asset_id === "USD")
            ?.amount,
    ).toBe("-101.00");
});

test("fictional explicit logout clears pending payment and ignores its late success after signing in again", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await setup(page, "receipt");
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route(`**${data.ledger}/business-payments`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        const response = await route.fetch();
        expect(response.status()).toBe(201);
        await gate;
        await route.fulfill({ response });
    });
    try {
        await submit(page);
        await expect
            .poll(
                async () =>
                    (
                        await api<BusinessPaymentSummary[]>(
                            page,
                            "GET",
                            `${data.ledger}/business-payments`,
                        )
                    ).length,
            )
            .toBe(1);
        await page.getByRole("button", { name: "Sign out", exact: true }).click();
        await expect(page.locator(".business-settlement-workspace")).toHaveCount(0);
        await expect(page.getByText("Fictional private payment edge", { exact: true })).toHaveCount(
            0,
        );
        await signIn(page);
        await page.getByRole("button", { name: "Parties and projects", exact: true }).click();
        const delivered = page.waitForResponse(
            (response) =>
                response.url().endsWith(`${data.ledger}/business-payments`) &&
                response.request().method() === "POST",
        );
        release();
        await delivered;
        await expect(
            page.getByRole("heading", { name: "Parties and projects", exact: true }).first(),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Retry original request", exact: true }),
        ).toHaveCount(0);
        const other = await api<Entity>(page, "POST", "/api/v1/entities", {
            kind: "personal",
            name: fictionalName("isolated settlement"),
            base_asset_id: "USD",
            template_key: "personal_default",
            locale: "en",
        });
        await page.goto(`/?entity=${other.id}&view=business-settlements`);
        await expect(page.getByText("No settlements on this page.", { exact: true })).toBeVisible();
        await expect(page.getByText("Fictional private payment edge", { exact: true })).toHaveCount(
            0,
        );
        const rows = await api<BusinessPaymentSummary[]>(
            page,
            "GET",
            `${data.ledger}/business-payments`,
        );
        expect(rows).toHaveLength(1);
        expect(
            (
                await page.request.get(
                    `/api/v1/ledgers/${other.ledger.id}/business-payments/${rows[0].id}`,
                )
            ).status(),
        ).toBe(404);
    } finally {
        release();
    }
});

test("fictional incomplete success receipt stays unknown until the full original receipt is replayed", async ({
    page,
}) => {
    const data = await setup(page, "receipt");
    const bodies: string[] = [],
        keys: string[] = [];
    await page.route(`**${data.ledger}/business-payments`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        bodies.push(route.request().postData()!);
        keys.push(route.request().headers()["idempotency-key"]);
        if (bodies.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(201);
            const incomplete = await response.json();
            delete incomplete.initial_allocation;
            return route.fulfill({ response, json: incomplete });
        }
        await route.continue();
    });
    await submit(page);
    await expect(
        page.getByRole("button", { name: "Retry original request", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("Saved; the complete original receipt is verified.", { exact: true }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Edit rejected request", exact: true }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(
        page.getByText("Saved; the complete original receipt is verified.", { exact: true }),
    ).toBeVisible();
    expect(bodies).toHaveLength(2);
    expect(new Set(bodies).size).toBe(1);
    expect(new Set(keys).size).toBe(1);
    expect(await api(page, "GET", `${data.ledger}/business-payments`)).toHaveLength(1);
});
