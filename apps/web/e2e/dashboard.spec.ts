import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Account, Category, Entity } from "../src/ledger-api";
import type { Reminder } from "../src/reminder-api";
import { api, expectNoOverflow, fictionalName, login, selectLedger } from "./helpers";

const ledgerPath = (entity: Entity) => `/api/v1/ledgers/${entity.ledger.id}`;

async function company(page: Page, description: string) {
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        name: fictionalName(description),
        kind: "company",
        country_code: "US",
        region_code: "NM",
        company_type: "llc",
        base_asset_id: "USD",
        template_key: "business_default",
        locale: "en",
    });
    const account = await api<Account>(page, "POST", ledgerPath(entity) + "/accounts", {
        name: fictionalName("dashboard bank"),
        kind: "bank",
        asset_ids: ["USD"],
    });
    return { entity, account };
}

test("the dashboard summarizes real records by ledger and recognition month", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await page.clock.setFixedTime(new Date("2026-11-15T12:00:00Z"));
    const fonts = new Set<string>();
    page.on("request", (request) => {
        if (request.resourceType() === "font") fonts.add(request.url());
    });
    await login(page);
    const payer = await company(page, "dashboard payer");
    const beneficiary = await company(page, "dashboard beneficiary");
    const base = ledgerPath(payer.entity);
    const categories = await api<Category[]>(page, "GET", base + "/categories");
    const alternate = await api<Account>(page, "POST", base + "/accounts", {
        name: fictionalName("dashboard cash"),
        kind: "cash",
        asset_ids: ["USD"],
    });
    const money = {
        account_id: payer.account.id,
        asset_id: "USD",
        transaction_date: "2026-11-05",
        description: "Fictional dashboard posting",
    };
    await api(page, "POST", base + "/opening-balances", { ...money, amount: "1000.00" });
    const income = categories.find((item) => item.kind === "income")!;
    await api(page, "POST", base + "/income", {
        ...money,
        amount: "123.00",
        recognition_date: "2026-11-04",
        splits: [{ category_id: income.id, amount: "123.00" }],
    });
    // Recognition dates, not transaction dates, put this income outside November.
    await api(page, "POST", base + "/income", {
        ...money,
        amount: "9.00",
        recognition_date: "2026-10-31",
        splits: [{ category_id: income.id, amount: "9.00" }],
    });
    await api(page, "POST", base + "/expenses", {
        ...money,
        amount: "30.45",
        recognition_date: "2026-11-04",
        splits: [
            {
                category_id: categories.find((item) => item.kind === "expense")!.id,
                amount: "30.45",
            },
        ],
    });
    await api(page, "POST", base + "/transfers", {
        source_account_id: payer.account.id,
        destination_account_id: alternate.id,
        asset_id: "USD",
        amount: "150.00",
        transaction_date: "2026-11-06",
        description: "Fictional internal transfer",
    });
    const beneficiaryExpense = (
        await api<Category[]>(page, "GET", ledgerPath(beneficiary.entity) + "/categories")
    ).find((item) => item.kind === "expense")!;
    await api(page, "PUT", "/api/v1/interledger/settings", { enabled: true });
    await api(page, "POST", "/api/v1/interledger/advances", {
        payer_ledger_id: payer.entity.ledger.id,
        beneficiary_ledger_id: beneficiary.entity.ledger.id,
        payer_account_id: payer.account.id,
        expense_category_id: beneficiaryExpense.id,
        asset_id: "USD",
        amount: "80.00",
        transaction_date: "2026-11-07",
        description: "Fictional dashboard receivable",
    });
    const addReminder = (title: string, date: string) =>
        api<Reminder>(page, "POST", base + "/reminders", {
            id: randomUUID(),
            event_kind: "certificate",
            title: fictionalName(title),
            manual_due_date: date,
            manual_reason: "Fictional dashboard reminder date",
        });
    const upcoming = await addReminder("upcoming certificate", "2026-11-23");
    const past = await addReminder("past certificate", "2026-11-04");

    await page.goto(`/?entity=${payer.entity.id}&view=overview`);
    const tile = (title: string) =>
        page
            .locator(".dashboard-tile")
            .filter({ has: page.getByRole("heading", { name: title, exact: true }) });
    const balance = tile("Original balances");
    const flow = tile("Operating income and expenses this month");
    const due = tile("Receivables and payables");
    await expect(balance.getByLabel("USD 1021.55", { exact: true })).toHaveText("$1,021.55");
    await expect(flow.getByLabel("USD 123.00", { exact: true })).toHaveText("$123.00");
    await expect(flow.getByLabel("USD 30.45", { exact: true })).toHaveAttribute(
        "data-negative",
        "true",
    );
    await expect(due.getByLabel("USD 80.00", { exact: true })).toBeVisible();
    await expect(tile("Upcoming reminders")).toContainText(upcoming.title);
    await expect(tile("Upcoming reminders")).not.toContainText(past.title);
    const capture = async (locale: "zh" | "en", width: number) => {
        if (process.env.COINPUP_CAPTURE_DASHBOARD === "1") {
            await page.screenshot({
                path: info.outputPath(`dashboard-${locale}-${width}.jpg`),
                fullPage: true,
                animations: "disabled",
                quality: 85,
                scale: "css",
            });
        }
    };
    for (const width of info.project.name.startsWith("mobile") ? [375, 320] : [1440]) {
        await page.setViewportSize({ width, height: 930 });
        await page.getByRole("button", { name: "中文", exact: true }).click();
        await expect(tile("原币余额").getByLabel("USD 1021.55", { exact: true })).toBeVisible();
        await page.evaluate(() => document.fonts.ready);
        await expectNoOverflow(page);
        await capture("zh", width);
        await page.getByRole("button", { name: "English", exact: true }).click();
        await expectNoOverflow(page);
        await capture("en", width);
    }
    expect([...fonts].some((url) => url.includes("noto-sans-sc") && url.endsWith(".woff2"))).toBe(
        true,
    );
    const pageOrigin = new URL(page.url()).origin;
    expect([...fonts].every((url) => new URL(url).origin === pageOrigin)).toBe(true);

    let release!: () => void;
    const hold = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route(
        (url) =>
            url.pathname === "/api/v1/reports/mvp" &&
            url.searchParams.get("ledger_ids") === beneficiary.entity.ledger.id,
        async (route) => {
            await hold;
            await route.continue();
        },
    );
    try {
        await selectLedger(page, beneficiary.entity.name);
        await expect(balance).toHaveAttribute("aria-busy", "true");
        await expect(balance.getByLabel("USD 1021.55", { exact: true })).toHaveCount(0);
    } finally {
        release();
    }
    await expect(balance).toHaveAttribute("aria-busy", "false");
    await expect(balance.getByLabel("USD 0.00", { exact: true })).toBeVisible();
    await expect(tile("Upcoming reminders")).not.toContainText(upcoming.title);
    await expect(due.getByRole("heading", { name: "Payable", exact: true })).toBeVisible();
});
