import { expect, test, type Page } from "@playwright/test";
import type {
    Account,
    Balance,
    Category,
    Entity,
    HistoryEntry,
    OperationResponse,
    OperationState,
} from "../src/ledger-api";
import { api, expectNoOverflow, fictionalName, login, selectLedger, username } from "./helpers";

type Fixture = {
    entity: Entity;
    account: Account;
    category: Category;
    receipt: OperationResponse;
    base: string;
    operation: string;
};

test.beforeEach(async ({ context }) => {
    await context.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
});

async function fixture(page: Page, withFee = false): Promise<Fixture> {
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("revision ledger"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const base = `/api/v1/ledgers/${entity.ledger.id}`;
    const account = await api<Account>(page, "POST", base + "/accounts", {
        name: fictionalName("revision wallet"),
        kind: "wise",
        asset_ids: withFee ? ["USD", "ETH"] : ["USD"],
    });
    const category = (await api<Category[]>(page, "GET", base + "/categories")).find(
        (item) => item.kind === "expense",
    )!;
    for (const [asset_id, amount] of withFee
        ? [
              ["USD", "1000.00"],
              ["ETH", "1.000000000000000000"],
          ]
        : [["USD", "1000.00"]]) {
        await api(page, "POST", base + "/opening-balances", {
            account_id: account.id,
            asset_id,
            amount,
            transaction_date: "2026-01-01",
            description: "Fictional revision opening fixture",
        });
    }
    const receipt = await api<OperationResponse>(page, "POST", base + "/expenses", {
        account_id: account.id,
        asset_id: "USD",
        amount: "100.00",
        transaction_date: "2026-10-03",
        recognition_date: "2026-09-30",
        description: "Fictional original purchase",
        splits: [{ category_id: category.id, amount: "100.00" }],
        fees: withFee
            ? [
                  {
                      account_id: account.id,
                      asset_id: "ETH",
                      amount: "0.000000000000000001",
                      category_id: category.id,
                  },
              ]
            : [],
    });
    return {
        entity,
        account,
        category,
        receipt,
        base,
        operation: `${base}/operations/${receipt.id}`,
    };
}

async function show(page: Page, data: Fixture): Promise<void> {
    await page.goto("/");
    await expect(page.getByTestId("current-username")).toHaveText(username);
    await selectLedger(page, data.entity.name);
    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await expect(page.getByTestId(`operation-${data.receipt.id}`)).toBeVisible();
}

async function correct(page: Page, data: Fixture): Promise<void> {
    await page
        .getByTestId(`operation-${data.receipt.id}`)
        .getByRole("button", { name: "Correct entry", exact: true })
        .click();
    await expect(page.getByRole("heading", { name: "Correct entry", exact: true })).toBeVisible();
}

async function changePrincipal(page: Page, amount: string, reason: string): Promise<void> {
    await page.getByLabel("Amount", { exact: true }).fill(amount);
    await page
        .getByTestId("split-row")
        .first()
        .getByLabel("Split amount", { exact: true })
        .fill(amount);
    await page.getByLabel("Revision reason", { exact: true }).fill(reason);
}

async function submitRevision(
    page: Page,
    data: Fixture,
    action: "corrections" | "cancellations",
): Promise<{ state: OperationState; body: Record<string, unknown> }> {
    const responsePromise = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === `${data.operation}/${action}` &&
            response.request().method() === "POST",
    );
    await page
        .getByRole("button", {
            name: action === "corrections" ? "Save correction" : "Confirm cancellation",
            exact: true,
        })
        .click();
    const response = await responsePromise;
    expect(response.status(), await response.text()).toBe(200);
    const state = (await response.json()) as OperationState;
    await expect(page.getByTestId(`operation-${data.receipt.id}`)).toContainText(
        `v${state.version}`,
    );
    return { state, body: response.request().postDataJSON() as Record<string, unknown> };
}

async function quantities(page: Page, data: Fixture): Promise<Record<string, string>> {
    const balances = await api<Balance[]>(page, "GET", data.base + "/balances");
    return Object.fromEntries(balances.map((item) => [item.asset_id, item.amount]));
}

test("correcting and cancelling a payment reverses every original fee and preserves visible history", async ({
    page,
}, testInfo) => {
    await login(page);
    const data = await fixture(page, true);
    await show(page, data);
    await correct(page, data);
    await expect(page.getByLabel("Entry type", { exact: true })).toBeDisabled();
    await expect(page.getByLabel("Account", { exact: true })).toHaveValue(data.account.id);
    await expect(page.getByLabel("Asset", { exact: true })).toHaveValue("USD");
    await expect(page.getByLabel("Amount", { exact: true })).toHaveValue("100.00");
    await expect(page.getByLabel("Transaction date", { exact: true })).toHaveValue("2026-10-03");
    await expect(page.getByLabel("Recognition date", { exact: true })).toHaveValue("2026-09-30");
    await expect(page.getByLabel("Description", { exact: true })).toHaveValue(
        "Fictional original purchase",
    );
    const fee = page.getByTestId("fee-row").first();
    await expect(fee.getByLabel("Fee account", { exact: true })).toHaveValue(data.account.id);
    await expect(fee.getByLabel("Fee asset", { exact: true })).toHaveValue("ETH");
    await expect(fee.getByLabel("Fee amount", { exact: true })).toHaveValue("0.000000000000000001");
    await page.getByRole("button", { name: "Save correction", exact: true }).click();
    expect((await api<OperationState>(page, "GET", data.operation)).version).toBe(1);
    await expect(page.getByLabel("Revision reason", { exact: true })).toHaveValue("");
    await changePrincipal(page, "120.00", "Fictional amount and fee correction");
    await fee.getByLabel("Fee amount", { exact: true }).fill("0.000000000000000002");
    await page.getByLabel("Recognition date", { exact: true }).fill("2026-09-29");
    await page.getByLabel("Description", { exact: true }).fill("Fictional corrected purchase");
    await page.getByRole("button", { name: "中文", exact: true }).click();
    await expect(page.getByLabel("修订原因", { exact: true })).toHaveValue(
        "Fictional amount and fee correction",
    );
    await expect(page.getByLabel("金额", { exact: true })).toHaveValue("120.00");
    await expectNoOverflow(page);
    await page.getByRole("button", { name: "English", exact: true }).click();
    const corrected = await submitRevision(page, data, "corrections");
    expect(corrected.state.id).toBe(data.receipt.id);
    expect(corrected.state.status).toBe("active");
    expect(corrected.body.expected_version).toBe(1);
    expect(corrected.body.replacement).not.toHaveProperty("id");
    expect(await quantities(page, data)).toEqual({ USD: "880.00", ETH: "0.999999999999999998" });
    // Old references may be archived or disabled; cancellation still reverses the old facts.
    await api(page, "PATCH", `${data.base}/accounts/${data.account.id}`, {
        expected_version: 1,
        archived: true,
        asset_ids: ["USD"],
    });
    await api(page, "PATCH", `${data.base}/categories/${data.category.id}`, {
        expected_version: 1,
        archived: true,
    });
    await show(page, data);
    await page
        .getByTestId(`operation-${data.receipt.id}`)
        .getByRole("button", { name: "Cancel entry", exact: true })
        .click();
    await page
        .getByLabel("Revision reason", { exact: true })
        .fill("Fictional duplicate cancellation");
    const cancelled = await submitRevision(page, data, "cancellations");
    expect(cancelled.state.status).toBe("cancelled");
    expect(cancelled.state.version).toBe(3);
    expect(cancelled.state.latest_posting).toEqual(corrected.state.latest_posting);
    expect(await quantities(page, data)).toEqual({ USD: "1000.00", ETH: "1.000000000000000000" });
    const row = page.getByTestId(`operation-${data.receipt.id}`);
    await expect(row).toContainText("Cancelled");
    await expect(row.getByRole("button", { name: "Correct entry", exact: true })).toBeDisabled();
    await expect(row.getByRole("button", { name: "Cancel entry", exact: true })).toBeDisabled();
    await row.getByRole("button", { name: "Version history", exact: true }).click();
    for (const version of [1, 2, 3])
        await expect(page.getByTestId(`version-${version}`)).toBeVisible();
    await expect(page.getByTestId("version-1")).toContainText("Fictional original purchase");
    await expect(page.getByTestId("version-2")).toContainText(
        "Fictional amount and fee correction",
    );
    await expect(page.getByTestId("version-2")).toContainText("-0.000000000000000002");
    await expect(page.getByTestId("version-3")).toContainText("Fictional duplicate cancellation");
    await expect(page.getByTestId("version-3")).toContainText("0.000000000000000002");
    const history = await api<HistoryEntry[]>(page, "GET", data.operation + "/history");
    expect(history.map((item) => [item.version, item.action])).toEqual([
        [1, "create"],
        [2, "correct"],
        [3, "cancel"],
    ]);
    expect(history[1].journals.map((item) => item.kind)).toEqual(["reversal", "posting"]);
    expect(history[1].journals[0].reverses_journal_id).toBe(history[0].journals[0].id);
    expect(history[1].journals[0].recognition_date).toBe("2026-09-30");
    expect(history[2].journals[0].reverses_journal_id).toBe(history[1].journals[1].id);
    expect(history[2].journals[0].recognition_date).toBe("2026-09-29");
    await expectNoOverflow(page);
    await page.screenshot({
        path: testInfo.outputPath("revision-and-fee-history.png"),
        fullPage: true,
    });
});

test("two revision windows preserve the losing draft until the user explicitly reloads the current entry", async ({
    page,
    context,
}) => {
    await login(page);
    const data = await fixture(page, true);
    await show(page, data);
    const second = await context.newPage();
    await show(second, data);
    await correct(page, data);
    await correct(second, data);
    await changePrincipal(page, "120.00", "Fictional winning correction");
    await changePrincipal(second, "130.00", "Fictional preserved losing draft");
    await submitRevision(page, data, "corrections");
    const rejectedPromise = second.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === data.operation + "/corrections" &&
            response.request().method() === "POST",
    );
    await second.getByRole("button", { name: "Save correction", exact: true }).click();
    const rejected = await rejectedPromise;
    expect(rejected.status()).toBe(409);
    expect((await rejected.json()).detail.code).toBe("version_conflict");
    expect(rejected.request().postDataJSON().expected_version).toBe(1);
    await expect(second.getByRole("alert")).toBeVisible();
    await expect(second.getByLabel("Amount", { exact: true })).toHaveValue("130.00");
    await expect(second.getByLabel("Revision reason", { exact: true })).toHaveValue(
        "Fictional preserved losing draft",
    );
    await expect(
        second.getByTestId("fee-row").first().getByLabel("Fee amount", { exact: true }),
    ).toHaveValue("0.000000000000000001");
    const current = await api<OperationState>(page, "GET", data.operation);
    expect(current.version).toBe(2);
    expect(current.latest_posting.kind === "expense" && current.latest_posting.amount).toBe(
        "120.00",
    );
    expect((await quantities(page, data)).USD).toBe("880.00");
    await second.getByRole("button", { name: "Reload current entry", exact: true }).click();
    await expect(second.getByLabel("Amount", { exact: true })).toHaveValue("120.00");
    await expect(second.getByLabel("Revision reason", { exact: true })).toHaveValue("");
    await changePrincipal(second, "130.00", "Fictional correction after explicit reload");
    const accepted = await submitRevision(second, data, "corrections");
    expect(accepted.body.expected_version).toBe(2);
    expect(accepted.state.id).toBe(data.receipt.id);
    expect(accepted.state.version).toBe(3);
    expect((await quantities(second, data)).USD).toBe("870.00");
    const history = await api<HistoryEntry[]>(second, "GET", data.operation + "/history");
    expect(history.map((item) => item.version)).toEqual([1, 2, 3]);
    expect(history[2].reason).toBe("Fictional correction after explicit reload");
    await expectNoOverflow(second);
    await second.close();
});

test("an unknown revision cannot be confirmed by an existing record and an old receipt never revives cancellation", async ({
    page,
}) => {
    test.setTimeout(60_000);
    await login(page);
    const data = await fixture(page);
    await show(page, data);
    await correct(page, data);
    await changePrincipal(page, "120.00", "Fictional uncertain correction");
    const endpoint = data.operation + "/corrections";
    const submissions: { key: string | undefined; body: string | null }[] = [];
    let originalRevision: OperationState | undefined;
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
                // This request does not reach the server: GET still returns the existing v1.
                await route.abort("connectionfailed");
            } else if (submissions.length === 2) {
                // The retry really commits v2, then its successful response is lost.
                const response = await route.fetch();
                expect(response.status(), await response.text()).toBe(200);
                originalRevision = (await response.json()) as OperationState;
                await route.abort("connectionfailed");
            } else await route.continue();
        },
    );
    await page.getByRole("button", { name: "Save correction", exact: true }).click();
    await expect(page.getByText("Save not confirmed", { exact: true })).toBeVisible();
    expect(submissions[0].key).toBeTruthy();
    const checkedPromise = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === data.operation &&
            response.request().method() === "GET",
    );
    await page.getByRole("button", { name: "Check entry status", exact: true }).click();
    expect((await (await checkedPromise).json()).version).toBe(1);
    await expect(
        page.getByRole("button", { name: "Check entry status", exact: true }),
    ).toBeEnabled();
    await expect(page.getByText("Save not confirmed", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Ledger", { exact: true })).toBeDisabled();
    expect(submissions).toHaveLength(1);
    await page.getByRole("button", { name: "Retry original submission", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original submission", exact: true }),
    ).toBeEnabled();
    expect(originalRevision?.version).toBe(2);
    expect(submissions).toHaveLength(2);
    expect(submissions[1]).toEqual(submissions[0]);
    // Another actual authenticated command advances the same stable operation to v3.
    const terminal = await api<OperationState>(page, "POST", data.operation + "/cancellations", {
        expected_version: 2,
        reason: "Fictional later cancellation from another device",
    });
    expect(terminal.status).toBe("cancelled");
    expect(terminal.version).toBe(3);
    const newerPromise = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === data.operation &&
            response.request().method() === "GET",
    );
    await page.getByRole("button", { name: "Check entry status", exact: true }).click();
    expect((await (await newerPromise).json()).version).toBe(3);
    await expect(
        page.getByRole("button", { name: "Check entry status", exact: true }),
    ).toBeEnabled();
    await expect(page.getByText("Save not confirmed", { exact: true })).toBeVisible();
    const replayPromise = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === endpoint &&
            response.request().method() === "POST" &&
            response.status() === 200,
    );
    await page.getByRole("button", { name: "Retry original submission", exact: true }).click();
    const historicalReceipt = (await (await replayPromise).json()) as OperationState;
    expect(historicalReceipt).toEqual(originalRevision);
    expect(historicalReceipt.version).toBe(2);
    expect(historicalReceipt.status).toBe("active");
    expect(submissions).toHaveLength(3);
    expect(submissions[2]).toEqual(submissions[0]);
    const row = page.getByTestId(`operation-${data.receipt.id}`);
    await expect(row).toContainText("Cancelled");
    await expect(row).toContainText("v3");
    await expect(row.getByRole("button", { name: "Correct entry", exact: true })).toBeDisabled();
    expect(await api<OperationState>(page, "GET", data.operation)).toEqual(terminal);
    expect(await quantities(page, data)).toEqual({ USD: "1000.00" });
    const history = await api<HistoryEntry[]>(page, "GET", data.operation + "/history");
    expect(history.map((item) => item.version)).toEqual([1, 2, 3]);
    await expectNoOverflow(page);
});
