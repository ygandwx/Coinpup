import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Account, Asset, AssetPreset, Balance, Category, Entity } from "../src/ledger-api";
import type { PlatformTransferDetail } from "../src/platform-transfers-api";
import { parseAmount } from "../src/money";
import { api, expectNoOverflow, fictionalName, login, selectLedger } from "./helpers";

const assetPath = "/api/v1/assets";
const platformAsset = "custodial:FDUSD";

async function entity(page: Page, name: string) {
    return api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName(name),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
}

function transferPath(item: Entity) {
    return `/api/v1/ledgers/${item.ledger.id}/platform-transfers`;
}

test("custodial asset has fixed catalog precision and a distinct identity", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    await page.getByRole("button", { name: "Assets", exact: true }).click();
    await page.getByRole("button", { name: "New asset", exact: true }).click();
    await page.getByLabel("Asset kind", { exact: true }).selectOption("custodial");
    const code = page.getByLabel("Asset code", { exact: true });
    await code.selectOption("POL");
    await expect(page.getByLabel("Decimal places", { exact: true })).toHaveValue("18");
    await expect(page.getByLabel("Decimal places", { exact: true })).not.toBeEditable();
    await expect(page.getByLabel("Network", { exact: true })).toHaveCount(0);
    const saving = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === assetPath &&
            response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Save", exact: true }).click();
    expect([201, 409]).toContain((await saving).status());
    const assets = await api<Asset[]>(page, "GET", assetPath);
    expect(assets).toEqual(
        expect.arrayContaining([
            expect.objectContaining({
                asset_id: "custodial:POL",
                kind: "custodial",
                network: null,
                scale: 18,
            }),
        ]),
    );
    await expectNoOverflow(page);
});

test("cross-ledger deposits retry the same command and withdrawals reverse as a group", async ({
    page,
}) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const [owner, receiver] = [await entity(page, "wallet"), await entity(page, "platform")];
    const presets = await api<AssetPreset[]>(page, "GET", `${assetPath}/presets`);
    const chain = presets.find((preset) => preset.code === "FDUSD" && preset.network === "ton");
    const custody = presets.find((preset) => preset.asset_id === platformAsset);
    expect(chain).toBeDefined();
    expect(custody).toBeDefined();
    if (!chain || !custody) throw new Error("Fixture requires FDUSD on TON and custodial FDUSD");
    for (const asset of [chain, custody]) {
        // Both browser projects share the disposable catalog.
        const { csrf_token } = await (await page.request.get("/api/v1/auth/session")).json();
        const reply = await page.request.post(assetPath, {
            headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": csrf_token },
            data: {
                code: asset.code,
                kind: asset.kind,
                network: asset.network,
                scale: asset.scale,
                token_reference: asset.token_reference,
            },
        });
        expect([201, 409], await reply.text()).toContain(reply.status());
    }

    const wallet = await api<Account>(page, "POST", `/api/v1/ledgers/${owner.ledger.id}/accounts`, {
        kind: "crypto",
        name: "Fictional Tonkeeper",
        custody_mode: "onchain",
        asset_ids: [chain.asset_id],
    });
    const platformName = "Fictional Binance";
    await page.reload();
    await selectLedger(page, receiver.name);
    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    await page.getByRole("button", { name: "New account", exact: true }).click();
    const accountForm = page.getByRole("dialog", { name: "New account", exact: true });
    await accountForm.getByLabel("Account name", { exact: true }).fill(platformName);
    await accountForm.getByRole("radio", { name: "Crypto wallet", exact: true }).check();
    await accountForm.getByLabel("Custody mode", { exact: true }).selectOption("custodial");
    await expect(accountForm.getByLabel(chain.asset_id, { exact: true })).toHaveCount(0);
    await accountForm.getByLabel(platformAsset, { exact: true }).check();
    await accountForm.getByRole("button", { name: "Save", exact: true }).click();
    await expect(accountForm).toHaveCount(0);
    const platform = (
        await api<Account[]>(page, "GET", `/api/v1/ledgers/${receiver.ledger.id}/accounts`)
    ).find((account) => account.name === platformName);
    expect(platform).toMatchObject({ custody_mode: "custodial", asset_ids: [platformAsset] });
    if (!platform) throw new Error("Custodial account missing");

    const expense = (
        await api<Category[]>(page, "GET", `/api/v1/ledgers/${owner.ledger.id}/categories`)
    ).find((category) => category.kind === "expense");
    expect(expense).toBeDefined();
    if (!expense) throw new Error("Fixture needs an expense category");
    await api(page, "POST", `/api/v1/ledgers/${owner.ledger.id}/opening-balances`, {
        account_id: wallet.id,
        asset_id: chain.asset_id,
        amount: "100",
        transaction_date: "2026-11-25",
    });

    await selectLedger(page, owner.name);
    await page.getByRole("button", { name: "Platform transfers", exact: true }).click();
    const panel = page.getByRole("region", { name: "Platform transfers", exact: true });
    await panel.getByRole("button", { name: "Record a deposit", exact: true }).click();
    const deposit = panel.getByRole("form", { name: "Record a deposit", exact: true });
    await deposit.getByLabel("Sending account", { exact: true }).selectOption(wallet.id);
    await deposit.getByLabel("Sending asset", { exact: true }).selectOption(chain.asset_id);
    await deposit.getByLabel("Receiving ledger", { exact: true }).selectOption(receiver.ledger.id);
    await deposit.getByLabel("Receiving account", { exact: true }).selectOption(platform.id);
    await deposit.getByLabel("Receiving asset", { exact: true }).selectOption(platformAsset);
    await deposit.getByLabel("Transfer quantity", { exact: true }).fill("10.123456");
    await deposit.getByLabel("Description", { exact: true }).fill("Fictional exchange top-up");
    await deposit.getByRole("button", { name: "Add fee", exact: true }).click();
    await deposit.getByLabel("Fee account", { exact: true }).selectOption(wallet.id);
    await deposit.getByLabel("Fee asset", { exact: true }).selectOption(chain.asset_id);
    await deposit.getByLabel("Fee quantity", { exact: true }).fill("0.05");
    await deposit.getByLabel("Fee category", { exact: true }).selectOption(expense.id);
    await expectNoOverflow(page);

    const sent: { key?: string; body: string | null }[] = [];
    await page.route(`**${transferPath(owner)}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        sent.push({
            key: route.request().headers()["idempotency-key"],
            body: route.request().postData(),
        });
        if (sent.length === 1) {
            const committed = await route.fetch();
            expect(committed.status(), await committed.text()).toBe(201);
            await route.abort("failed");
        } else await route.continue();
    });
    await deposit.getByRole("button", { name: "Record transfer", exact: true }).click();
    await expect(panel.getByRole("alert")).toContainText("unconfirmed");
    await panel.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(
        panel.getByText("The transfer postings are confirmed.", { exact: true }),
    ).toBeVisible();
    expect(sent).toHaveLength(2);
    expect(sent[0]?.key).toBeTruthy();
    expect(sent[1]).toEqual(sent[0]);
    const depositRecords = await api<PlatformTransferDetail[]>(page, "GET", transferPath(receiver));
    expect(depositRecords).toHaveLength(1);
    expect(depositRecords[0]?.operations).toHaveLength(2);
    expect(depositRecords[0]?.fees).toHaveLength(1);

    await selectLedger(page, receiver.name);
    await panel.getByRole("button", { name: "View details", exact: true }).click();
    await expect(panel.getByText(/original sending ledger/)).toBeVisible();
    await expect(
        panel.getByRole("button", { name: "Cancel entire transfer", exact: true }),
    ).toHaveCount(0);
    await panel.getByRole("button", { name: "Close details", exact: true }).click();
    await panel.getByRole("button", { name: "Record a withdrawal", exact: true }).click();
    const withdrawal = panel.getByRole("form", { name: "Record a withdrawal", exact: true });
    await withdrawal.getByLabel("Sending account", { exact: true }).selectOption(platform.id);
    await withdrawal.getByLabel("Sending asset", { exact: true }).selectOption(platformAsset);
    await withdrawal.getByLabel("Receiving ledger", { exact: true }).selectOption(owner.ledger.id);
    await withdrawal.getByLabel("Receiving account", { exact: true }).selectOption(wallet.id);
    await withdrawal.getByLabel("Receiving asset", { exact: true }).selectOption(chain.asset_id);
    const amount = withdrawal.getByLabel("Transfer quantity", { exact: true });
    await amount.fill("0.1234567");
    await withdrawal.getByRole("button", { name: "Record transfer", exact: true }).click();
    await expect(panel.getByRole("alert")).toContainText("exact on both sides");
    await amount.fill("0.123456");
    await withdrawal.getByRole("button", { name: "Record transfer", exact: true }).click();
    await expect(
        panel.getByText("The transfer postings are confirmed.", { exact: true }),
    ).toBeVisible();
    const details = panel.getByRole("region", { name: "Transfer details", exact: true });
    await details.getByRole("button", { name: "Cancel entire transfer", exact: true }).click();
    await details.getByLabel("Cancellation reason", { exact: true }).fill("Fictional reversal");
    await details.getByRole("button", { name: "Confirm entire reversal", exact: true }).click();
    await expect(
        panel.getByText("The entire transfer reversal is confirmed.", { exact: true }),
    ).toBeVisible();
    await expect(details.getByText("Fictional reversal", { exact: true })).toBeVisible();
    const histories = await api<PlatformTransferDetail[]>(page, "GET", transferPath(owner));
    expect(histories).toHaveLength(2);
    const cancelled = histories.find((row) => row.kind === "withdrawal");
    expect(cancelled).toMatchObject({ status: "cancelled", version: 2 });
    expect(cancelled?.cancellation?.reversals).toHaveLength(2);
    const balances = await api<Balance[]>(
        page,
        "GET",
        `/api/v1/ledgers/${owner.ledger.id}/balances`,
    );
    expect(
        parseAmount(
            balances.find((balance) => balance.account_id === wallet.id)!.amount,
            chain.scale,
        ),
    ).toBe(parseAmount("89.826544", chain.scale));
    await expectNoOverflow(page);
});
