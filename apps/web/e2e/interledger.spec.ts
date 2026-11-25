import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { InterledgerDetail } from "../src/interledger-api";
import type { Account, Balance, Category, ControlBalance, Entity } from "../src/ledger-api";
import { api, expectNoOverflow, fictionalName, login } from "./helpers";

const advances = "/api/v1/interledger/advances";
const path = (entity: Entity) => `/api/v1/ledgers/${entity.ledger.id}`;

async function ledger(page: Page, kind: "personal" | "company", amount: string) {
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        name: fictionalName(`interledger ${kind}`),
        kind,
        ...(kind === "company" ? { country_code: "HK", company_type: "private_limited" } : {}),
        base_asset_id: "USD",
        template_key: kind === "personal" ? "personal_default" : "business_default",
        locale: "en",
    });
    const account = await api<Account>(page, "POST", path(entity) + "/accounts", {
        name: fictionalName("interledger bank"),
        kind: "bank",
        asset_ids: ["USD"],
    });
    await api(page, "POST", path(entity) + "/opening-balances", {
        account_id: account.id,
        asset_id: "USD",
        amount,
        transaction_date: "2026-11-01",
        description: "Fictional advance fixture opening",
    });
    return { entity, account };
}

test("an advance and two repayments retain both postings and history when disabled", async ({
    page,
    context,
}) => {
    await context.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
    await login(page);
    const payer = await ledger(page, "personal", "1000.00");
    const benefit = await ledger(page, "company", "2000.00");
    const category = (
        await api<Category[]>(page, "GET", path(benefit.entity) + "/categories")
    ).find(({ kind }) => kind === "expense")!;
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    const field = (name: string) => page.getByLabel(name, { exact: true });
    const detail = page.getByRole("region", { name: "Advance details", exact: true });
    const outstanding = page.getByTestId("interledger-outstanding");
    const getBalance = async (item: typeof payer) => {
        const rows = await api<Balance[]>(page, "GET", path(item.entity) + "/balances");
        return rows.find(
            ({ account_id, asset_id }) => account_id === item.account.id && asset_id === "USD",
        )?.amount;
    };
    const control = async (item: typeof payer, key: ControlBalance["system_key"]) => {
        const rows = await api<ControlBalance[]>(
            page,
            "GET",
            path(item.entity) + "/control-balances",
        );
        return rows.find(({ system_key, asset_id }) => system_key === key && asset_id === "USD")
            ?.amount;
    };
    await page.goto(`/?entity=${payer.entity.id}&view=interledger`);
    const setting = page.getByRole("button", { name: /^(Enable|Disable) advances$/u });
    await expect(setting).toBeVisible();
    if ((await setting.textContent())?.trim() === "Enable advances")
        await button("Enable advances").click();
    await button("New advance").click();
    await field("Original payer ledger").selectOption(payer.entity.ledger.id);
    await field("Beneficiary ledger").selectOption(benefit.entity.ledger.id);
    await field("Original asset").selectOption("USD");
    await field("Original payer money account").selectOption(payer.account.id);
    await field("Beneficiary expense category").selectOption(category.id);
    await field("Amount").fill("100.0");
    await field("Transaction date").fill("2026-11-03");
    await field("Description").fill("Fictional bilingual interledger advance");
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await button("中文").click();
        await expect(field("金额")).toHaveValue("100.0");
        await expectNoOverflow(page);
        await button("English").click();
        await expect(field("Amount")).toHaveValue("100.0");
        await expectNoOverflow(page);
    }
    const writes: Array<{ key: string; body: string | null }> = [];
    let advanceId = "";
    await page.route("**" + advances, async (route) => {
        const request = route.request();
        if (request.method() !== "POST") return route.continue();
        writes.push({ key: request.headers()["idempotency-key"], body: request.postData() });
        if (writes.length > 1) return route.continue();
        const response = await route.fetch();
        expect(response.status()).toBe(201);
        advanceId = (await response.json()).id;
        await route.abort("failed");
    });
    await button("Register advance").click();
    await expect(button("Retry original request")).toBeVisible();
    await expect(button("Control balances")).toBeDisabled();
    await button("Retry original request").click();
    await expect(outstanding).toHaveText("100.00 USD");
    expect(advanceId).toBeTruthy();
    expect(writes).toEqual([writes[0], writes[0]]);
    expect(writes[0].key).toBeTruthy();
    expect(await getBalance(payer)).toBe("900.00");
    expect(await getBalance(benefit)).toBe("2000.00");
    expect(await control(payer, "intercompany.receivable")).toBe("100.00");
    expect(await control(benefit, "intercompany.payable")).toBe("-100.00");
    for (const [amount, remainder] of [
        ["40.00", "60.00"],
        ["60.00", "0.00"],
    ]) {
        await button("New repayment").click();
        await field("Beneficiary payment account").selectOption(benefit.account.id);
        await field("Original payer receipt account").selectOption(payer.account.id);
        await field("Amount").fill(amount);
        await field("Transaction date").fill("2026-11-04");
        await button("Record repayment").click();
        await expect(outstanding).toHaveText(remainder + " USD");
    }
    expect(await getBalance(payer)).toBe("1000.00");
    expect(await getBalance(benefit)).toBe("1900.00");
    expect(await control(payer, "intercompany.receivable")).toBe("0.00");
    expect(await control(benefit, "intercompany.payable")).toBe("0.00");
    const row = await api<InterledgerDetail>(page, "GET", `${advances}/${advanceId}`);
    expect(row.repayments.map(({ amount }) => amount).sort()).toEqual(["40.00", "60.00"]);
    expect(
        new Set([
            row.payer_operation_id,
            row.beneficiary_operation_id,
            ...row.repayments.flatMap((item) => [
                item.payer_operation_id,
                item.beneficiary_operation_id,
            ]),
        ]).size,
    ).toBe(6);
    await button("Disable advances").click();
    await expect(button("Enable advances")).toBeVisible();
    await expect(detail).toContainText(advanceId);
    await expect(detail).toContainText("40.00 USD");
    await expect(detail).toContainText("60.00 USD");
    await expect(button("New advance")).toHaveCount(0);
});
