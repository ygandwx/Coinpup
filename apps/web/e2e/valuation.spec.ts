import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login } from "./helpers";
import type { Account, Category, Entity } from "../src/ledger-api";
import type { InterledgerAdvance, InterledgerRepayment } from "../src/interledger-api";

const advances = "/api/v1/interledger/advances";
const quotes = "/api/v1/quotes/manual";
const ledgerPath = (item: Entity) => `/api/v1/ledgers/${item.ledger.id}`;

async function company(page: Page, suffix: string, usd: string, eur?: string) {
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        name: fictionalName("valuation " + suffix),
        kind: "company",
        country_code: "HK",
        company_type: "private_limited",
        base_asset_id: "USD",
        template_key: "business_default",
        locale: "en",
    });
    const account = await api<Account>(page, "POST", ledgerPath(entity) + "/accounts", {
        name: fictionalName("valuation bank " + suffix),
        kind: "bank",
        asset_ids: eur ? ["USD", "EUR"] : ["USD"],
    });
    for (const [asset_id, amount] of [
        ["USD", usd],
        ["EUR", eur],
    ]) {
        if (!amount) continue;
        await api(page, "POST", ledgerPath(entity) + "/opening-balances", {
            account_id: account.id,
            asset_id,
            amount,
            transaction_date: "2026-01-01",
            description: "Fictional opening",
        });
    }
    return { entity, account };
}

test("consolidates selected ledgers, prices exact dates and shows the original and excluded rows", async ({
    page,
    context,
}, info) => {
    await context.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
    await login(page);
    const payer = await company(page, "payer", "1000");
    const beneficiary = await company(page, "beneficiary", "2000", "10");
    const category = (
        await api<Category[]>(page, "GET", ledgerPath(beneficiary.entity) + "/categories")
    ).find(({ kind }) => kind === "expense")!;
    await api(page, "PUT", "/api/v1/interledger/settings", { enabled: true });
    const advance = await api<InterledgerAdvance>(page, "POST", advances, {
        payer_ledger_id: payer.entity.ledger.id,
        beneficiary_ledger_id: beneficiary.entity.ledger.id,
        payer_account_id: payer.account.id,
        expense_category_id: category.id,
        asset_id: "USD",
        amount: "100",
        transaction_date: "2026-04-02",
        description: "Fictional external supplier advance",
    });
    const repayment = await api<InterledgerRepayment>(
        page,
        "POST",
        `${advances}/${advance.id}/repayments`,
        {
            payer_receipt_account_id: payer.account.id,
            beneficiary_payment_account_id: beneficiary.account.id,
            amount: "40",
            transaction_date: "2026-04-03",
            description: "Fictional internal partial repayment",
        },
    );
    await page.goto(`/?entity=${payer.entity.id}&view=valuation`);
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    const field = (name: string) => page.getByLabel(name, { exact: true });
    const original = (id: string) => page.getByTestId("valuation-original").filter({ hasText: id });
    const summary = page.locator(".valuation-details summary");
    const valuation = page.getByRole("region", { name: "Valuation result", exact: true });
    const day = 1 + info.retry * 2 + (info.project.name.startsWith("mobile") ? 12 : 0);
    const priceDate = `2026-07-${String(day).padStart(2, "0")}`;
    await button("Run report").click();
    await expect(original("USD")).toContainText("940.00 USD");
    await field("Ledger").selectOption(beneficiary.entity.id);
    await expect(field(payer.entity.name!)).toBeChecked();
    await expect(field(beneficiary.entity.name!)).not.toBeChecked();
    await expect(original("USD")).toContainText("940.00 USD");
    await field(beneficiary.entity.name!).check();
    await button("Run report").click();
    await expect(original("USD")).toContainText("2,900.00 USD");
    await expect(original("EUR")).toContainText("10.00 EUR");
    await field("Value in").selectOption("GBP");
    await field("Price date").fill(priceDate);
    await button("Run report").click();
    await expect(valuation).toContainText("prices are missing");
    await expect(valuation).toContainText("EUR");
    await expect(valuation).toContainText("USD");

    await field("Quote date").fill(priceDate);
    await field("Quote currency").selectOption("GBP");
    await field("Priced asset").selectOption("USD");
    await field("Price per unit").fill("0.7500");
    await field("Price source").fill("Fictional manual USD quote");
    const attempted: Array<string | null> = [];
    let resolveDropped!: () => void;
    let rejectDropped!: (error: unknown) => void;
    const lostResponse = new Promise<void>((resolve, reject) => {
        resolveDropped = resolve;
        rejectDropped = reject;
    });
    await page.route("**" + quotes, async (route) => {
        if (route.request().method() !== "PUT") return route.continue();
        attempted.push(route.request().postData());
        if (attempted.length > 1) return route.continue();
        try {
            const response = await route.fetch();
            expect(response.status()).toBe(200);
            await route.abort("failed");
            resolveDropped();
        } catch (error) {
            rejectDropped(error);
        }
    });
    await Promise.all([lostResponse, button("Save price").click()]);
    await expect(button("Retry original save")).toBeVisible();
    await expect(button("Advances and repayments")).toBeDisabled();
    await button("Retry original save").click();
    await expect(page.getByText("Price saved.", { exact: true })).toBeVisible();
    expect(attempted).toEqual([attempted[0], attempted[0]]);
    expect(JSON.parse(attempted[0]!).price).toBe("0.7500");
    await expect(valuation).toContainText("EUR");
    await expect(valuation).not.toContainText("USD");
    await button("Add another price").click();
    await field("Priced asset").selectOption("EUR");
    await field("Price per unit").fill("2");
    await field("Price source").fill("Fictional manual EUR quote");
    await button("Save price").click();
    await expect(valuation).toContainText("2,195.00 GBP");
    await summary.filter({ hasText: "EUR" }).click();
    await expect(
        page.getByText("Fictional manual EUR quote", { exact: false }).first(),
    ).toBeVisible();

    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await button("中文").click();
        await expect(field("指定报价日期")).toHaveValue(priceDate);
        await expect(page.getByRole("region", { name: "折算结果", exact: true })).toContainText(
            "2,195.00 GBP",
        );
        await expectNoOverflow(page);
        await button("English").click();
        await expect(valuation).toContainText("2,195.00 GBP");
        await expectNoOverflow(page);
    }

    await field("Price per unit").fill("99999999999999999999");
    await button("Save price").click();
    await expect(valuation).toContainText("1,000,000,000,000,000,002,165.00 GBP");
    await expectNoOverflow(page);

    await field("Value in").selectOption("");
    await field("Report view").selectOption("cash");
    await field("From date").fill("2026-04-01");
    await field("Through date").fill("2026-04-30");
    await button("Run report").click();
    await expect(original("USD")).toContainText("-100.00 USD");
    await expect(original("USD")).toContainText("Inflow 0.00 USD");
    await expect(summary.filter({ hasText: "Internal repayment excluded" })).toHaveCount(2);
    const excluded = summary.filter({ hasText: "Internal repayment excluded" }).first();
    await excluded.click();
    await expect(excluded.locator("..")).toContainText(repayment.id);
    await field(beneficiary.entity.name!).uncheck();
    await button("Run report").click();
    await expect(original("USD")).toContainText("-60.00 USD");
    await expect(original("USD")).toContainText("Inflow 40.00 USD");
    await expect(summary.filter({ hasText: "Internal repayment excluded" })).toHaveCount(0);
    await expect(summary.filter({ hasText: "Internal repayment" })).toHaveCount(1);

    await field(beneficiary.entity.name!).check();
    await field("Report view").selectOption("operating");
    await field("From date").fill("2026-04-01");
    await field("Through date").fill("2026-04-30");
    await button("Run report").click();
    await expect(original("USD")).toContainText("Expenses 100.00 USD");
    await expect(summary).toHaveCount(1);
    await expect(summary).toContainText(beneficiary.entity.name!);
});
