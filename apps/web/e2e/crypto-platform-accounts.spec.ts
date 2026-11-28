import { randomBytes, randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import { protectFixtureValues, test } from "./private-account-test";
import type { Locator, Page } from "@playwright/test";
import type { Account, Asset, AssetPreset, Balance, Entity } from "../src/ledger-api";
import type { PlatformTransferDetail } from "../src/platform-transfers-api";
import { parseAmount } from "../src/money";
import { api, expectNoOverflow, fictionalName, login, selectLedger } from "./helpers";

const prefix = (entity: Entity) => `/api/v1/ledgers/${entity.ledger.id}`;
const usdt = "custodial:USDT";
const btc = "custodial:BTC";

async function openNewAccount(page: Page, entity: Entity, name: string, custodial: boolean) {
    await selectLedger(page, entity.name);
    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    await page.getByRole("button", { name: "New account", exact: true }).click();
    const editor = page.getByRole("dialog", { name: "New account", exact: true });
    await editor.getByLabel("Account name", { exact: true }).fill(name);
    await editor.getByRole("radio", { name: "Crypto account", exact: true }).check();
    await editor
        .getByRole("radio", {
            name: custodial ? "Custodial platform" : "Self-custody wallet",
            exact: true,
        })
        .check();
    return editor;
}

async function pickPlatform(editor: Locator, name: string) {
    await editor.getByLabel("Search platforms", { exact: true }).fill(name);
    await editor.getByRole("radio", { name, exact: true }).check();
}

async function savedAccount(page: Page, editor: Locator, entity: Entity) {
    const sent = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === prefix(entity) + "/accounts" &&
            response.request().method() === "POST",
    );
    await editor.getByRole("button", { name: "Save", exact: true }).click();
    const reply = await sent;
    expect(reply.status()).toBe(201);
    await expect(editor).toHaveCount(0);
    return { account: (await reply.json()) as Account, sent: reply.request().postDataJSON() };
}

async function ledger(page: Page, label: string) {
    return api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName(label),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
}

test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
});

test("Binance stores BTC and USDT profiles separately and trades USDT custody with a TRON wallet", async ({
    page,
}) => {
    const owner = await ledger(page, "platform");
    const walletOwner = await ledger(page, "chain wallet");
    const uid = fictionalName("UID");
    const email = `fictional-${randomUUID()}@example.invalid`;
    const memo = fictionalName("memo");
    const fictionalTronAddress = "41" + randomBytes(20).toString("hex");
    protectFixtureValues(uid, email, memo, fictionalTronAddress);
    const presets = await api<AssetPreset[]>(page, "GET", "/api/v1/assets/presets");
    const chain = presets.find(
        (preset) => preset.kind === "token" && preset.code === "USDT" && preset.network === "tron",
    );
    expect(chain).toBeDefined();
    if (!chain) throw new Error("The verified TRON USDT preset is missing");
    const loadedCatalog = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return (
            response.request().method() === "GET" &&
            response.ok() &&
            url.pathname === "/api/v1/assets" &&
            url.searchParams.has("include_disabled")
        );
    });
    await page.reload();
    const currentAssets: Asset[] = await (await loadedCatalog).json();

    const editor = await openNewAccount(page, owner, "Fictional trading reserves", true);
    await pickPlatform(editor, "Binance");
    await expect(editor.locator('label:has(input[value="binance"]) img')).toBeVisible();
    await editor.getByLabel(usdt, { exact: true }).check();
    await editor.getByLabel(btc, { exact: true }).check();
    await editor.getByLabel("UID or account name", { exact: true }).fill(uid);
    await editor.getByLabel("Linked email", { exact: true }).fill(email);
    const usdtProfile = editor.getByTestId(`coin-profile-${usdt}`);
    const btcProfile = editor.getByTestId(`coin-profile-${btc}`);
    await usdtProfile.locator("summary").click();
    await usdtProfile.getByLabel("Deposit network", { exact: true }).selectOption("tron");
    await usdtProfile.getByLabel("Deposit address", { exact: true }).fill(fictionalTronAddress);
    await usdtProfile.getByLabel("Memo / Tag", { exact: true }).fill(memo);
    await usdtProfile.getByLabel("Label", { exact: true }).fill("Fictional TRON settlement");
    await btcProfile.locator("summary").click();
    await btcProfile.getByLabel("Deposit network", { exact: true }).selectOption("bitcoin");
    await btcProfile.getByLabel("Label", { exact: true }).fill("Fictional BTC reserve");
    await expectNoOverflow(page);
    const { account: binance, sent } = await savedAccount(page, editor, owner);
    const missing = [btc, usdt].filter(
        (id) => !currentAssets.some(({ asset_id }) => id === asset_id),
    );
    expect([...(sent.crypto_preset_asset_ids ?? [])].sort()).toEqual(missing.sort());
    expect(binance.crypto_platform?.institution_id).toBe("binance");
    expect(binance.crypto_platform?.uid === uid).toBe(true);
    expect(binance.crypto_platform?.email === email).toBe(true);
    const savedBtc = binance.crypto_assets?.find((entry) => entry.asset_id === btc);
    expect(savedBtc?.details.network).toBe("bitcoin");
    expect(savedBtc?.details.label).toBe("Fictional BTC reserve");
    const savedUsdt = binance.crypto_assets?.find((entry) => entry.asset_id === usdt);
    expect(savedUsdt?.details.network).toBe("tron");
    expect(savedUsdt?.details.address === fictionalTronAddress).toBe(true);
    expect(savedUsdt?.details.memo === memo).toBe(true);

    const walletEditor = await openNewAccount(page, walletOwner, "Fictional on-chain TRON", false);
    await pickPlatform(walletEditor, "TronLink");
    await walletEditor.getByLabel("Choose network", { exact: true }).selectOption("tron");
    await walletEditor.getByLabel(chain.asset_id, { exact: true }).check();
    const walletDetails = walletEditor.getByTestId(`coin-profile-${chain.asset_id}`);
    await walletDetails.locator("summary").click();
    const walletAddress = walletDetails.getByLabel("Deposit address", { exact: true });
    await walletAddress.fill("not-a-TRON-address");
    await walletEditor.getByRole("button", { name: "Save", exact: true }).click();
    await expect(walletEditor.getByRole("alert")).toContainText("selected network");
    await expect(walletAddress).toHaveValue("not-a-TRON-address");
    await walletAddress.fill("41" + "1".repeat(40));
    const { account: wallet } = await savedAccount(page, walletEditor, walletOwner);
    await api(page, "POST", prefix(walletOwner) + "/opening-balances", {
        account_id: wallet.id,
        asset_id: chain.asset_id,
        amount: "82.75",
        transaction_date: "2026-11-25",
    });
    await page.getByRole("button", { name: "Platform transfers", exact: true }).click();
    const transfers = page.getByRole("region", { name: "Platform transfers", exact: true });
    async function transfer(direction: "deposit" | "withdrawal", amount: string) {
        const label = direction === "deposit" ? "Record a deposit" : "Record a withdrawal";
        await transfers.getByRole("button", { name: label, exact: true }).click();
        const form = transfers.getByRole("form", { name: label, exact: true });
        const deposit = direction === "deposit";
        await form
            .getByLabel("Sending account", { exact: true })
            .selectOption(deposit ? wallet.id : binance.id);
        const sendingInstitution = deposit ? "tronlink" : "binance";
        await expect(
            form.locator(`.account-picker-selected [data-institution="${sendingInstitution}"] img`),
        ).toBeVisible();
        await form
            .getByLabel("Sending asset", { exact: true })
            .selectOption(deposit ? chain!.asset_id : usdt);
        await form
            .getByLabel("Receiving ledger", { exact: true })
            .selectOption((deposit ? owner : walletOwner).ledger.id);
        await form
            .getByLabel("Receiving account", { exact: true })
            .selectOption(deposit ? binance.id : wallet.id);
        await form
            .getByLabel("Receiving asset", { exact: true })
            .selectOption(deposit ? usdt : chain!.asset_id);
        await form.getByLabel("Transfer quantity", { exact: true }).fill(amount);
        await form.getByRole("button", { name: "Record transfer", exact: true }).click();
        await expect(
            transfers.getByText("The transfer postings are confirmed.", { exact: true }),
        ).toBeVisible();
    }

    await transfer("deposit", "12.125001");
    await selectLedger(page, owner.name);
    await transfer("withdrawal", "2.125001");
    const details = transfers.getByRole("region", { name: "Transfer details", exact: true });
    await details.getByRole("button", { name: "Cancel entire transfer", exact: true }).click();
    await details
        .getByLabel("Cancellation reason", { exact: true })
        .fill("Fictional transfer reversal");
    await details.getByRole("button", { name: "Confirm entire reversal", exact: true }).click();
    await expect(
        transfers.getByText("The entire transfer reversal is confirmed.", { exact: true }),
    ).toBeVisible();

    const records = await api<PlatformTransferDetail[]>(
        page,
        "GET",
        prefix(walletOwner) + "/platform-transfers",
    );
    expect(records.find((row) => row.kind === "withdrawal")).toMatchObject({
        status: "cancelled",
        version: 2,
    });
    const balances = await api<Balance[]>(page, "GET", prefix(owner) + "/balances");
    expect(
        parseAmount(
            balances.find((row) => row.account_id === binance.id && row.asset_id === usdt)!.amount,
            6,
        ),
    ).toBe(parseAmount("12.125001", 6));

    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    const card = page.getByTestId(`account-${binance.id}`);
    await expect(card.locator('[data-institution="binance"] .institution-logo')).toBeVisible();
    await card.getByText("Coins and balances", { exact: true }).click();
    await expect(card.locator('[data-asset-code="BTC"] [data-network="bitcoin"]')).toBeVisible();
    await expect(card.locator('[data-asset-code="USDT"] [data-network="tron"]')).toBeVisible();
    await expect(card.getByText("12.125001", { exact: true })).toBeVisible();
    await expect(card.getByText("Fictional BTC reserve", { exact: true })).toBeVisible();

    await card.getByRole("button", { name: "Edit", exact: true }).click();
    const edit = page.getByRole("dialog", { name: "Edit account", exact: true });
    expect((await edit.getByLabel("UID or account name").inputValue()) === uid).toBe(true);
    expect((await edit.getByLabel("Linked email").inputValue()) === email).toBe(true);
    const editUsdt = edit.getByTestId(`coin-profile-${usdt}`);
    await editUsdt.locator("summary").click();
    expect(
        (await editUsdt.getByLabel("Deposit address").inputValue()) === fictionalTronAddress,
    ).toBe(true);
    expect((await editUsdt.getByLabel("Memo / Tag").inputValue()) === memo).toBe(true);
    const editBtc = edit.getByTestId(`coin-profile-${btc}`);
    await editBtc.locator("summary").click();
    await editBtc.getByLabel("Label", { exact: true }).fill("Fictional BTC reserve renamed");
    await edit.getByRole("button", { name: "Save", exact: true }).click();
    await expect(edit).toHaveCount(0);
    const stored = (await api<Account[]>(page, "GET", prefix(owner) + "/accounts")).find(
        ({ id }) => id === binance.id,
    );
    expect(stored?.version).toBe(binance.version + 1);
    expect(stored?.crypto_assets?.find((entry) => entry.asset_id === btc)?.details.label).toBe(
        "Fictional BTC reserve renamed",
    );
    const retainedUsdt = stored?.crypto_assets?.find((entry) => entry.asset_id === usdt);
    expect(retainedUsdt?.details.address === fictionalTronAddress).toBe(true);
    expect(retainedUsdt?.details.memo === memo).toBe(true);
});

test("a custom wallet uses an initial and keeps its existing unconfigured crypto accounts", async ({
    page,
}) => {
    const owner = await ledger(page, "custom wallet");
    const old = await api<Account>(page, "POST", prefix(owner) + "/accounts", {
        name: "Fictional existing wallet",
        kind: "crypto",
        asset_ids: ["BTC"],
        details: { wallet_label: "Legacy marker" },
    });
    await page.reload();
    const editor = await openNewAccount(page, owner, "MetaMask account backup", false);
    await editor.getByRole("radio", { name: "Other", exact: true }).check();
    await editor.getByLabel("Platform name", { exact: true }).fill("Seastar lab");
    await editor.getByLabel("Choose network", { exact: true }).selectOption("bitcoin");
    await editor.getByLabel("BTC", { exact: true }).check();
    const { account } = await savedAccount(page, editor, owner);
    expect(account.crypto_platform).toMatchObject({ custom_name: "Seastar lab" });
    await expect(
        page.getByTestId(`account-${account.id}`).locator(".institution-mark-text"),
    ).toHaveText("S");
    await expect(
        page.getByTestId(`account-${account.id}`).locator('[data-institution="metamask"]'),
    ).toHaveCount(0);
    const legacy = page.getByTestId(`account-${old.id}`);
    await legacy.getByRole("button", { name: "Edit", exact: true }).click();
    const edit = page.getByRole("dialog", { name: "Edit account", exact: true });
    await expect(
        edit.getByRole("button", { name: "Configure platform and coins", exact: true }),
    ).toBeVisible();
    await edit.getByLabel("Account name", { exact: true }).fill("Fictional renamed old wallet");
    await edit.getByRole("button", { name: "Save", exact: true }).click();
    await expect(edit).toHaveCount(0);
    const stored = (await api<Account[]>(page, "GET", prefix(owner) + "/accounts")).find(
        ({ id }) => id === old.id,
    );
    expect(stored).toMatchObject({
        name: "Fictional renamed old wallet",
        details: { wallet_label: "Legacy marker" },
        crypto_platform: null,
    });
});
