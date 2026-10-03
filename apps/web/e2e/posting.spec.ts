import { randomUUID } from "node:crypto";
import { expect, test, type Locator, type Page } from "@playwright/test";
import type {
    Account,
    Asset,
    Balance,
    Category,
    Entity,
    FinancialResponse,
    HistoryEntry,
    OperationState,
} from "../src/ledger-api";
import { api, expectNoOverflow, fictionalName, login, selectLedger, username } from "./helpers";

type EntryKind = FinancialResponse["kind"];

test.beforeEach(async ({ context }) => {
    await context.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
});

async function ledger(page: Page): Promise<Entity> {
    return api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("posting ledger"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
}

function path(entity: Entity, suffix: string): string {
    return `/api/v1/ledgers/${entity.ledger.id}${suffix}`;
}

async function account(
    page: Page,
    entity: Entity,
    kind: Account["kind"],
    assets: string[],
): Promise<Account> {
    return api<Account>(page, "POST", path(entity, "/accounts"), {
        name: fictionalName(kind),
        kind,
        asset_ids: assets,
    });
}

async function openLedger(page: Page, entity: Entity, section = "Transactions"): Promise<void> {
    await page.reload();
    await expect(page.getByTestId("current-username")).toHaveText(username);
    await selectLedger(page, entity.name);
    await page.getByRole("button", { name: section, exact: true }).click();
}

async function newEntry(page: Page, kind: EntryKind): Promise<void> {
    await page.getByRole("button", { name: "New entry", exact: true }).click();
    await page.getByLabel("Entry type", { exact: true }).selectOption(kind);
    await page.getByLabel("Transaction date", { exact: true }).fill("2026-10-03");
    if (kind === "income" || kind === "expense") {
        await page.getByLabel("Recognition date", { exact: true }).fill("2026-09-30");
    }
}

async function save<T>(
    page: Page,
    endpoint: string,
    method: "POST" | "PATCH" = "POST",
): Promise<T> {
    const responsePromise = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === endpoint && response.request().method() === method,
    );
    await page.getByRole("button", { name: "Save", exact: true }).click();
    const response = await responsePromise;
    expect(response.status(), await response.text()).toBe(method === "POST" ? 201 : 200);
    return response.json() as Promise<T>;
}

async function saveEntry(page: Page, entity: Entity, kind: EntryKind): Promise<FinancialResponse> {
    const suffix = {
        opening: "/opening-balances",
        income: "/income",
        expense: "/expenses",
        transfer: "/transfers",
        exchange: "/exchanges",
    }[kind];
    const receipt = await save<FinancialResponse>(page, path(entity, suffix));
    await expect(page.getByTestId(`operation-${receipt.id}`)).toBeVisible();
    return receipt;
}

async function setQuantity(
    page: Page,
    target: Account,
    asset: string,
    amount: string,
): Promise<void> {
    await page.getByLabel("Account", { exact: true }).selectOption(target.id);
    await page.getByLabel("Asset", { exact: true }).selectOption(asset);
    await page.getByLabel("Amount", { exact: true }).fill(amount);
}

async function setSplit(row: Locator, category: Category, amount: string): Promise<void> {
    await row.getByLabel("Category", { exact: true }).selectOption(category.id);
    await row.getByLabel("Split amount", { exact: true }).fill(amount);
}

async function addFee(
    page: Page,
    target: Account,
    asset: string,
    amount: string,
    category: Category,
): Promise<void> {
    await page.getByRole("button", { name: "Add fee", exact: true }).click();
    const row = page.getByTestId("fee-row").last();
    await row.getByLabel("Fee account", { exact: true }).selectOption(target.id);
    await row.getByLabel("Fee asset", { exact: true }).selectOption(asset);
    await row.getByLabel("Fee amount", { exact: true }).fill(amount);
    await row.getByLabel("Fee category", { exact: true }).selectOption(category.id);
}

async function seedOpening(
    page: Page,
    entity: Entity,
    target: Account,
    asset: string,
    amount: string,
): Promise<void> {
    await api(page, "POST", path(entity, "/opening-balances"), {
        account_id: target.id,
        asset_id: asset,
        amount,
        transaction_date: "2026-01-01",
        description: "Fictional browser opening fixture",
    });
}

async function operations(page: Page, entity: Entity): Promise<OperationState[]> {
    return api<OperationState[]>(page, "GET", path(entity, "/operations?limit=100&offset=0"));
}

async function quantities(page: Page, entity: Entity): Promise<Record<string, string>> {
    const values = await api<Balance[]>(page, "GET", path(entity, "/balances?limit=100&offset=0"));
    return Object.fromEntries(
        values.map((value) => [`${value.account_id}:${value.asset_id}`, value.amount]),
    );
}

test("real opening positions and split income and expense retain original quantities and dates", async ({
    page,
}) => {
    await login(page);
    const entity = await ledger(page);
    const other = await ledger(page);
    const wallet = await account(page, entity, "wise", ["USD", "EUR", "ETH"]);
    const categories = await api<Category[]>(page, "GET", path(entity, "/categories"));
    const expenses = categories.filter((item) => item.kind === "expense");
    const incomes = categories.filter((item) => item.kind === "income");
    expect(incomes.length).toBeGreaterThanOrEqual(2);
    await openLedger(page, entity);
    for (const [asset, amount] of [
        ["USD", "100.00"],
        ["EUR", "80.00"],
        ["ETH", "1.000000000000000001"],
    ]) {
        await newEntry(page, "opening");
        await setQuantity(page, wallet, asset, amount);
        const opening = await saveEntry(page, entity, "opening");
        expect(opening.kind).toBe("opening");
    }
    await newEntry(page, "expense");
    await setQuantity(page, wallet, "USD", "25.00");
    await page.getByLabel("Description", { exact: true }).fill("Fictional 软件 / software split");
    await setSplit(page.getByTestId("split-row").first(), expenses[0], "10.00");
    await page.getByRole("button", { name: "Add split", exact: true }).click();
    await setSplit(page.getByTestId("split-row").nth(1), expenses[1], "14.00");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    expect(await operations(page, entity)).toHaveLength(3);
    await expect(page.getByLabel("Amount", { exact: true })).toHaveValue("25.00");
    await page
        .getByTestId("split-row")
        .nth(1)
        .getByLabel("Split amount", { exact: true })
        .fill("15.00");
    await page.getByRole("button", { name: "中文", exact: true }).click();
    await expect(page.getByLabel("金额", { exact: true })).toHaveValue("25.00");
    await expect(page.getByLabel("备注", { exact: true })).toHaveValue(
        "Fictional 软件 / software split",
    );
    await expectNoOverflow(page);
    await page.getByRole("button", { name: "English", exact: true }).click();
    const expense = await saveEntry(page, entity, "expense");
    expect(expense.transaction_date).toBe("2026-10-03");
    expect(expense.recognition_date).toBe("2026-09-30");
    await newEntry(page, "income");
    await setQuantity(page, wallet, "EUR", "10.00");
    await setSplit(page.getByTestId("split-row").first(), incomes[0], "6.00");
    await page.getByRole("button", { name: "Add split", exact: true }).click();
    await setSplit(page.getByTestId("split-row").nth(1), incomes[1], "4.00");
    await saveEntry(page, entity, "income");
    expect(await quantities(page, entity)).toEqual({
        [`${wallet.id}:USD`]: "75.00",
        [`${wallet.id}:EUR`]: "90.00",
        [`${wallet.id}:ETH`]: "1.000000000000000001",
    });
    await openLedger(page, entity, "Accounts");
    const row = page.getByTestId(`account-${wallet.id}`);
    for (const amount of ["75.00", "90.00", "1.000000000000000001"])
        await expect(row).toContainText(amount);
    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await expect(page.getByTestId(`operation-${expense.id}`)).toContainText("25.00");
    await selectLedger(page, other.name);
    await expect(page.getByTestId(`operation-${expense.id}`)).toHaveCount(0);
    expect(await operations(page, other)).toEqual([]);
    await expectNoOverflow(page);
});

test("credit card repayment and cash transfer change balances without creating a second expense", async ({
    page,
}) => {
    await login(page);
    const entity = await ledger(page);
    const bank = await account(page, entity, "bank", ["CNY"]);
    const card = await account(page, entity, "credit_card", ["CNY"]);
    const cash = await account(page, entity, "cash", ["CNY"]);
    await seedOpening(page, entity, bank, "CNY", "1000.00");
    const category = (await api<Category[]>(page, "GET", path(entity, "/categories"))).find(
        (item) => item.kind === "expense",
    )!;
    await openLedger(page, entity);
    await newEntry(page, "expense");
    await setQuantity(page, card, "CNY", "100.00");
    await setSplit(page.getByTestId("split-row").first(), category, "100.00");
    await saveEntry(page, entity, "expense");
    expect((await quantities(page, entity))[`${card.id}:CNY`]).toBe("-100.00");
    for (const [destination, amount] of [
        [card, "100.00"],
        [cash, "200.00"],
    ] as const) {
        await newEntry(page, "transfer");
        await page.getByLabel("Source account", { exact: true }).selectOption(bank.id);
        await page.getByLabel("Destination account", { exact: true }).selectOption(destination.id);
        await page.getByLabel("Asset", { exact: true }).selectOption("CNY");
        await page.getByLabel("Amount", { exact: true }).fill(amount);
        const receipt = await saveEntry(page, entity, "transfer");
        expect(receipt.kind).toBe("transfer");
    }
    expect(await quantities(page, entity)).toEqual({
        [`${bank.id}:CNY`]: "700.00",
        [`${card.id}:CNY`]: "0.00",
        [`${cash.id}:CNY`]: "200.00",
    });
    const records = await operations(page, entity);
    expect(records.filter((item) => item.kind === "expense")).toHaveLength(1);
    expect(records.filter((item) => item.kind === "income")).toHaveLength(0);
    expect(records.filter((item) => item.kind === "transfer")).toHaveLength(2);
    const expense = records.find((item) => item.kind === "expense")!.latest_posting;
    expect(expense.kind === "expense" && expense.amount).toBe("100.00");
    for (const record of records.filter((item) => item.kind === "transfer")) {
        const history = await api<HistoryEntry[]>(
            page,
            "GET",
            path(entity, `/operations/${record.id}/history`),
        );
        expect(history[0].journals[0].lines.map((line) => line.role)).toEqual([
            "account",
            "account",
        ]);
    }
    await openLedger(page, entity, "Accounts");
    await expect(page.getByTestId(`account-${bank.id}`)).toContainText("700.00");
    await expect(page.getByTestId(`account-${card.id}`)).toContainText("0.00");
    await expect(page.getByTestId(`account-${cash.id}`)).toContainText("200.00");
    await expectNoOverflow(page);
});

test("exchange actual quantities and fees in a third asset remain separate and atomic", async ({
    page,
}, testInfo) => {
    await login(page);
    const entity = await ledger(page);
    const wallet = await account(page, entity, "wise", ["USD", "EUR"]);
    const feeAccount = await account(page, entity, "bank", ["GBP"]);
    await seedOpening(page, entity, wallet, "USD", "1000.00");
    await seedOpening(page, entity, feeAccount, "GBP", "10.00");
    const category = (await api<Category[]>(page, "GET", path(entity, "/categories"))).find(
        (item) => item.kind === "expense",
    )!;
    await openLedger(page, entity);
    await newEntry(page, "exchange");
    await page.getByLabel("Source account", { exact: true }).selectOption(wallet.id);
    await page.getByLabel("Destination account", { exact: true }).selectOption(wallet.id);
    await page.getByLabel("Source asset", { exact: true }).selectOption("USD");
    await page.getByLabel("Destination asset", { exact: true }).selectOption("EUR");
    await page.getByLabel("Source amount", { exact: true }).fill("100.00");
    await page.getByLabel("Destination amount", { exact: true }).fill("90.00");
    await addFee(page, wallet, "USD", "2.00", category);
    await addFee(page, feeAccount, "GBP", "0", category);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    expect(await operations(page, entity)).toHaveLength(2);
    expect(await quantities(page, entity)).toEqual({
        [`${wallet.id}:USD`]: "1000.00",
        [`${wallet.id}:EUR`]: "0.00",
        [`${feeAccount.id}:GBP`]: "10.00",
    });
    await page.getByTestId("fee-row").nth(1).getByLabel("Fee amount", { exact: true }).fill("0.25");
    await expectNoOverflow(page);
    const receipt = await saveEntry(page, entity, "exchange");
    expect(receipt.kind).toBe("exchange");
    if (receipt.kind !== "exchange") throw new Error("Expected an actual exchange receipt");
    expect(receipt.source_amount).toBe("100.00");
    expect(receipt.destination_amount).toBe("90.00");
    expect(receipt.fees?.map((fee) => [fee.account_id, fee.asset_id, fee.amount])).toEqual([
        [wallet.id, "USD", "2.00"],
        [feeAccount.id, "GBP", "0.25"],
    ]);
    expect(await quantities(page, entity)).toEqual({
        [`${wallet.id}:USD`]: "898.00",
        [`${wallet.id}:EUR`]: "90.00",
        [`${feeAccount.id}:GBP`]: "9.75",
    });
    const history = await api<HistoryEntry[]>(
        page,
        "GET",
        path(entity, `/operations/${receipt.id}/history`),
    );
    const expenseLines = history[0].journals[0].lines.filter((line) => line.role === "expense");
    expect(expenseLines.map((line) => [line.asset_id, line.amount])).toEqual([
        ["USD", "2.00"],
        ["GBP", "0.25"],
    ]);
    expect(history[0].journals[0].lines.some((line) => line.role === "income")).toBe(false);
    await page.reload();
    await expect(page.getByTestId(`operation-${receipt.id}`)).toContainText("90.00");
    await page.getByTestId(`operation-${receipt.id}`).locator("summary").click();
    await expect(
        page.getByTestId(`operation-${receipt.id}`).getByText("0.25", { exact: true }),
    ).toBeVisible();
    await expectNoOverflow(page);
    await page.screenshot({
        path: testInfo.outputPath("exchange-and-third-asset-fee.png"),
        fullPage: true,
    });
});

test("a committed crypto payment survives a lost response and real 401 with the original request", async ({
    page,
}) => {
    test.setTimeout(60_000);
    await login(page);
    const entity = await ledger(page);
    const wallet = await account(page, entity, "crypto", ["BTC"]);
    await seedOpening(page, entity, wallet, "BTC", "1.00000000");
    const category = (await api<Category[]>(page, "GET", path(entity, "/categories"))).find(
        (item) => item.kind === "expense",
    )!;
    await openLedger(page, entity);
    await newEntry(page, "expense");
    await setQuantity(page, wallet, "BTC", "0.10000000");
    await setSplit(page.getByTestId("split-row").first(), category, "0.10000000");
    await addFee(page, wallet, "BTC", "0.00001000", category);
    const endpoint = path(entity, "/expenses");
    const submissions: { key: string | undefined; body: string | null }[] = [];
    let original: FinancialResponse | undefined;
    await page.route(
        (url) => url.pathname === endpoint,
        async (route) => {
            const request = route.request();
            if (request.method() !== "POST") {
                await route.continue();
                return;
            }
            submissions.push({
                key: request.headers()["idempotency-key"],
                body: request.postData(),
            });
            if (submissions.length === 1) {
                // The real transaction commits first. Only the browser's response is lost.
                const response = await route.fetch();
                expect(response.status(), await response.text()).toBe(201);
                original = (await response.json()) as FinancialResponse;
                await route.abort("connectionfailed");
            } else await route.continue();
        },
    );
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Save not confirmed", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Ledger", { exact: true })).toBeDisabled();
    expect(original).toBeDefined();
    expect(submissions[0].key).toBeTruthy();
    await page.getByRole("button", { name: "中文", exact: true }).click();
    await expect(page.getByText("保存结果待确认", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "English", exact: true }).click();
    const session = await (await page.request.get("/api/v1/auth/session")).json();
    const logout = await page.request.post("/api/v1/auth/logout", {
        headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": session.csrf_token },
    });
    expect(logout.status()).toBe(204);
    const unauthorized = page.waitForResponse(
        (response) => new URL(response.url()).pathname === endpoint && response.status() === 401,
    );
    await page.getByRole("button", { name: "Retry original submission", exact: true }).click();
    expect((await unauthorized).status()).toBe(401);
    await expect(page.getByRole("heading", { name: "Welcome back", exact: true })).toBeVisible();
    await expect(page.getByTestId("current-username")).toHaveCount(0);
    expect(submissions).toHaveLength(2);
    expect(submissions[1]).toEqual(submissions[0]);
    // Reauthenticate in this document; a page navigation would discard in-memory recovery.
    const password = process.env.E2E_PASSWORD;
    if (!password) throw new Error("A disposable E2E_PASSWORD is required");
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByTestId("current-username")).toHaveText(username);
    await expect(page.getByText("Save not confirmed", { exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Retry original submission", exact: true }),
    ).toBeEnabled();
    expect(submissions).toHaveLength(2);
    const replayPromise = page.waitForResponse(
        (response) => new URL(response.url()).pathname === endpoint && response.status() === 201,
    );
    await page.getByRole("button", { name: "Retry original submission", exact: true }).click();
    const replay = (await (await replayPromise).json()) as FinancialResponse;
    expect(replay).toEqual(original);
    expect(submissions).toHaveLength(3);
    expect(submissions[2]).toEqual(submissions[0]);
    await expect(page.getByTestId(`operation-${replay.id}`)).toHaveCount(1);
    expect(await quantities(page, entity)).toEqual({ [`${wallet.id}:BTC`]: "0.89999000" });
    expect(await operations(page, entity)).toHaveLength(2);
    const state = await api<OperationState>(page, "GET", path(entity, `/operations/${replay.id}`));
    expect(state.latest_posting).toEqual(original);
    const history = await api<HistoryEntry[]>(
        page,
        "GET",
        path(entity, `/operations/${replay.id}/history`),
    );
    expect(history).toHaveLength(1);
    expect(history[0].journals).toHaveLength(1);
    expect(history[0].journals[0].id).toBe(replay.journal_id);
    await expectNoOverflow(page);
});

test("a stablecoin needs explicit UI configuration and retains exact history when disabled", async ({
    page,
}) => {
    await login(page);
    const entity = await ledger(page);
    const wallet = await account(page, entity, "crypto", ["USD"]);
    const category = (await api<Category[]>(page, "GET", path(entity, "/categories"))).find(
        (item) => item.kind === "income",
    )!;
    const network = `fictional-e2e-${randomUUID().slice(0, 8)}`;
    const reference = "FictionalUSDCOnly";
    const expectedId = `token:${network}:${reference}`;
    await openLedger(page, entity);
    await newEntry(page, "income");
    await page.getByLabel("Account", { exact: true }).selectOption(wallet.id);
    expect(
        await page
            .getByLabel("Asset", { exact: true })
            .locator("option")
            .evaluateAll((nodes) => nodes.map((node) => (node as HTMLOptionElement).value)),
    ).not.toContain(expectedId);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByRole("button", { name: "Assets", exact: true }).click();
    await page.getByRole("button", { name: "New asset", exact: true }).click();
    await page.getByLabel("Asset kind", { exact: true }).selectOption("token");
    await page.getByLabel("Asset code", { exact: true }).selectOption("USDC");
    await page.getByLabel("Network", { exact: true }).fill(network);
    await page.getByLabel("Token reference", { exact: true }).fill(reference);
    await page.getByLabel("Decimal places", { exact: true }).fill("6");
    await page.getByLabel("Enabled", { exact: true }).check();
    const asset = await save<Asset>(page, "/api/v1/assets");
    expect(asset.asset_id).toBe(expectedId);
    expect(asset.scale).toBe(6);
    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    await page
        .getByTestId(`account-${wallet.id}`)
        .getByRole("button", { name: "Edit", exact: true })
        .click();
    await page.getByLabel(asset.asset_id, { exact: true }).check();
    await save<Account>(page, path(entity, `/accounts/${wallet.id}`), "PATCH");
    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await newEntry(page, "income");
    await setQuantity(page, wallet, asset.asset_id, "1.2345678");
    await setSplit(page.getByTestId("split-row").first(), category, "1.2345678");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    expect(await operations(page, entity)).toEqual([]);
    await page.getByLabel("Amount", { exact: true }).fill("1.234567");
    await page
        .getByTestId("split-row")
        .first()
        .getByLabel("Split amount", { exact: true })
        .fill("1.234567");
    const receipt = await saveEntry(page, entity, "income");
    expect(receipt.kind === "income" && receipt.amount).toBe("1.234567");
    await page.getByRole("button", { name: "Assets", exact: true }).click();
    const assetRow = page.getByTestId(`asset-${asset.asset_id}`);
    await assetRow.getByRole("button", { name: "Disable", exact: true }).click();
    await expect(assetRow.getByRole("button", { name: "Enable", exact: true })).toBeEnabled();
    const balances = await api<Balance[]>(page, "GET", path(entity, "/balances"));
    const tokenBalance = balances.find((item) => item.asset_id === asset.asset_id)!;
    expect(tokenBalance.amount).toBe("1.234567");
    expect(tokenBalance.asset_enabled).toBe(false);
    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    await expect(page.getByTestId(`account-${wallet.id}`)).toContainText("1.234567");
    await expect(page.getByTestId(`account-${wallet.id}`)).toContainText("Disabled");
    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await newEntry(page, "income");
    await page.getByLabel("Account", { exact: true }).selectOption(wallet.id);
    const selectable = await page
        .getByLabel("Asset", { exact: true })
        .locator("option:not(:disabled)")
        .evaluateAll((nodes) => nodes.map((node) => (node as HTMLOptionElement).value));
    expect(selectable).not.toContain(asset.asset_id);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByRole("button", { name: "Assets", exact: true }).click();
    await assetRow.getByRole("button", { name: "Enable", exact: true }).click();
    await expect(assetRow.getByRole("button", { name: "Disable", exact: true })).toBeEnabled();
    await expectNoOverflow(page);
});
