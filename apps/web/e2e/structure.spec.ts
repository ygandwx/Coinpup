import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login, selectLedger, username } from "./helpers";

type Entity = {
    id: string;
    name: string;
    country_code: string | null;
    region_code: string | null;
    company_type: string | null;
    details: Record<string, string>;
    ledger: { id: string };
};
type Account = {
    id: string;
    name: string;
    asset_ids: string[];
    kind: string;
    details: Record<string, string>;
    archived: boolean;
    version: number;
};
type Category = {
    id: string;
    name: string;
    kind: string;
    parent_id: string | null;
    archived: boolean;
};
type Balance = { account_id: string; asset_id: string; amount: string; account_archived: boolean };

test.beforeEach(async ({ context }) => {
    await context.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
});

async function save<T>(page: Page, path: string, method: "POST" | "PATCH" = "POST"): Promise<T> {
    const responsePromise = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === path && response.request().method() === method,
    );
    await page.getByRole("button", { name: "Save", exact: true }).click();
    const response = await responsePromise;
    expect(response.status(), await response.text()).toBe(method === "POST" ? 201 : 200);
    return response.json() as Promise<T>;
}

async function personal(page: Page, name: string, template = "personal_default"): Promise<Entity> {
    return api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name,
        base_asset_id: "USD",
        template_key: template,
        locale: "en",
    });
}

async function openLedger(page: Page, entity: Entity, section: string): Promise<void> {
    await page.reload();
    await expect(page.getByTestId("current-username")).toHaveText(username);
    await selectLedger(page, entity.name);
    await page.getByRole("button", { name: section, exact: true }).click();
}

test("company forms follow regional fields and preserve a bilingual draft through a real save", async ({
    page,
}, testInfo) => {
    await login(page);
    await page.getByRole("button", { name: /New ledger$/ }).click();
    await expect(
        page.getByRole("heading", { name: "New ledger", level: 1, exact: true }),
    ).toBeFocused();
    const name = fictionalName("HongKong").replace(/\s/g, "").padEnd(160, "X");
    await page.getByLabel("Display name", { exact: true }).fill(name);
    await expect(page.getByRole("link", { name: "Coinpup", exact: true })).toHaveCount(0);
    const draftUrl = page.url();
    await page.locator(".topbar .brand").click();
    await expect(page).toHaveURL(draftUrl);
    await expect(page.getByLabel("Display name", { exact: true })).toHaveValue(name);
    await page.getByLabel("Entity type", { exact: true }).selectOption("company");
    const country = page.getByLabel("Country / region", { exact: true });
    await country.selectOption("CN");
    await expect(page.getByLabel("Unified Social Credit Code", { exact: true })).toBeVisible();
    await expect(
        page.getByLabel("Business Registration Number (BRN)", { exact: true }),
    ).toHaveCount(0);
    await country.selectOption("US");
    await expect(page.getByLabel("EIN", { exact: true })).toBeVisible();
    const state = page.getByLabel("State", { exact: true });
    await state.selectOption("NM");
    await expect(state).toHaveValue("NM");
    await state.selectOption("WY");
    await expect(state).toHaveValue("WY");
    await country.selectOption("EE");
    await expect(page.getByLabel("Registry code", { exact: true })).toBeVisible();
    await expect(page.getByLabel("VAT identification number", { exact: true })).toBeVisible();
    await expect(state).toHaveCount(0);
    await country.selectOption("HK");
    await page.getByLabel("Legal name", { exact: true }).fill("Fictional Hong Kong Limited");
    await page.getByLabel("Registration date", { exact: true }).fill("2026-01-15");
    await page
        .getByLabel("Business Registration Number (BRN)", { exact: true })
        .fill("FICTIONAL-BRN-ONLY");
    await page.getByLabel("Base currency", { exact: true }).selectOption("HKD");
    await page.getByLabel("Category template", { exact: true }).selectOption("business_default");
    await page.getByRole("button", { name: "中文", exact: true }).click();
    await expect(page.getByLabel("显示名称", { exact: true })).toHaveValue(name);
    await expect(page.getByLabel("商业登记号码（BRN）", { exact: true })).toHaveValue(
        "FICTIONAL-BRN-ONLY",
    );
    await expect(page.getByLabel("注册国家／地区", { exact: true })).toHaveValue("HK");
    await expectNoOverflow(page);
    await page.getByRole("button", { name: "English", exact: true }).click();
    await expect(page.getByLabel("Display name", { exact: true })).toHaveValue(name);
    const created = await save<Entity>(page, "/api/v1/entities");
    expect(created.name).toBe(name);
    expect(created.country_code).toBe("HK");
    expect(created.region_code).toBeNull();
    expect(created.company_type).toBe("private_limited");
    expect(Object.values(created.details)).toContain("FICTIONAL-BRN-ONLY");
    await openLedger(page, created, "Ledger details");
    await expect(page.getByRole("heading", { name, level: 2, exact: true })).toBeVisible();
    await expect(page.getByText("FICTIONAL-BRN-ONLY", { exact: true })).toBeVisible();
    await expectNoOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("regional-ledger.png"), fullPage: true });
    await page.getByRole("button", { name: "Edit details", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Edit details", level: 1, exact: true }),
    ).toBeFocused();
    await expect(page.getByLabel("Display name", { exact: true })).toHaveValue(name);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Ledger details", level: 1, exact: true }),
    ).toBeFocused();
});

test("a personal template copy stays independent while parent and child categories are edited and archived", async ({
    page,
}) => {
    await login(page);
    await page.getByRole("button", { name: /New ledger$/ }).click();
    await page.getByLabel("Display name", { exact: true }).fill(fictionalName("personal"));
    await page.getByLabel("Entity type", { exact: true }).selectOption("personal");
    await page.getByLabel("Base currency", { exact: true }).selectOption("USD");
    await page.getByLabel("Category template", { exact: true }).selectOption("business_default");
    const first = await save<Entity>(page, "/api/v1/entities");
    const second = await personal(page, fictionalName("independent template"), "business_default");
    const categoriesPath = `/api/v1/ledgers/${first.ledger.id}/categories`;
    const original = await api<Category[]>(page, "GET", categoriesPath);
    const separatePath = `/api/v1/ledgers/${second.ledger.id}/categories`;
    const separateBefore = await api<Category[]>(page, "GET", separatePath);
    expect(original.length).toBeGreaterThan(0);
    expect(original.map((item) => item.name).sort()).toEqual(
        separateBefore.map((item) => item.name).sort(),
    );
    expect(original.every((item) => !separateBefore.some((other) => other.id === item.id))).toBe(
        true,
    );
    const parent = original.find((item) => item.kind === "expense" && item.parent_id === null)!;
    expect(parent).toBeDefined();
    await openLedger(page, first, "Categories");
    await page
        .getByTestId(`category-${parent.id}`)
        .getByRole("button", { name: "Edit", exact: true })
        .click();
    const renamed = fictionalName("renamed parent");
    await page.getByLabel("Category name", { exact: true }).fill(renamed);
    await save<Category>(page, `${categoriesPath}/${parent.id}`, "PATCH");
    await page.getByRole("button", { name: "New category", exact: true }).click();
    const childName = fictionalName("child");
    await page.getByLabel("Category name", { exact: true }).fill(childName);
    await page.getByLabel("Type", { exact: true }).selectOption("expense");
    await page.getByLabel("Parent category", { exact: true }).selectOption(parent.id);
    const child = await save<Category>(page, categoriesPath);
    expect(child.parent_id).toBe(parent.id);
    const row = page.getByTestId(`category-${child.id}`);
    await expect(row).toContainText(childName);
    await row.getByRole("button", { name: "Edit", exact: true }).click();
    await page.getByLabel("Category name", { exact: true }).fill(childName + " revised");
    await save<Category>(page, `${categoriesPath}/${child.id}`, "PATCH");
    await expect(row).toContainText(childName + " revised");
    await row.getByRole("button", { name: "Archive", exact: true }).click();
    await expect(row).toHaveCount(0);
    await page.getByLabel("Show archived", { exact: true }).check();
    await row.getByRole("button", { name: "Restore", exact: true }).click();
    await expect(row.getByRole("button", { name: "Archive", exact: true })).toBeVisible();
    const restored = (await api<Category[]>(page, "GET", categoriesPath)).find(
        (item) => item.id === child.id,
    )!;
    expect(restored.archived).toBe(false);
    expect(restored.name).toBe(childName + " revised");
    expect(restored.parent_id).toBe(parent.id);
    expect(await api<Category[]>(page, "GET", separatePath)).toEqual(separateBefore);
    await selectLedger(page, second.name);
    await expect(page.getByTestId(`category-${child.id}`)).toHaveCount(0);
    await expect(page.getByTestId(`category-${separateBefore[0].id}`)).toBeVisible();
    await selectLedger(page, first.name);
    await expect(page.getByTestId(`category-${child.id}`)).toContainText(childName + " revised");
    await expectNoOverflow(page);
});

test("multiasset accounts display exact real balances after refresh and archival without crossing ledgers", async ({
    page,
}, testInfo) => {
    await login(page);
    const first = await personal(page, fictionalName("multiasset"));
    const other = await personal(page, fictionalName("other ledger"));
    const otherAccount = await api<Account>(
        page,
        "POST",
        `/api/v1/ledgers/${other.ledger.id}/accounts`,
        {
            name: fictionalName("other account"),
            kind: "cash",
            asset_ids: ["USD"],
        },
    );
    await openLedger(page, first, "Accounts");
    await page.getByRole("button", { name: "New account", exact: true }).click();
    await page.getByLabel("Account name", { exact: true }).fill(fictionalName("Wise"));
    await page.getByRole("radio", { name: "Wise", exact: true }).check();
    await page.getByLabel("Show all assets", { exact: true }).check();
    for (const asset of ["USD", "EUR", "ETH"])
        await page.getByLabel(asset, { exact: true }).check();
    await expectNoOverflow(page);
    const account = await save<Account>(page, `/api/v1/ledgers/${first.ledger.id}/accounts`);
    expect([...account.asset_ids].sort()).toEqual(["ETH", "EUR", "USD"]);
    const expectedAmounts = { USD: "75.00", EUR: "90.00", ETH: "1.000000000000000001" };
    // Seed financial facts through the real API; this increment has no posting form.
    for (const [asset_id, amount] of Object.entries(expectedAmounts)) {
        await api(page, "POST", `/api/v1/ledgers/${first.ledger.id}/opening-balances`, {
            account_id: account.id,
            asset_id,
            amount,
            transaction_date: "2026-01-01",
            description: "Fictional browser balance fixture",
        });
    }
    await openLedger(page, first, "Accounts");
    const row = page.getByTestId(`account-${account.id}`);
    for (const amount of Object.values(expectedAmounts)) await expect(row).toContainText(amount);
    await expect(page.getByTestId(`account-${otherAccount.id}`)).toHaveCount(0);
    await selectLedger(page, other.name);
    await expect(page.getByTestId(`account-${otherAccount.id}`)).toBeVisible();
    await expect(row).toHaveCount(0);
    await selectLedger(page, first.name);
    await row.getByRole("button", { name: "Archive", exact: true }).click();
    await expect(row).toHaveCount(0);
    await page.getByLabel("Show archived", { exact: true }).check();
    await expect(row.getByRole("button", { name: "Restore", exact: true })).toBeVisible();
    for (const amount of Object.values(expectedAmounts)) await expect(row).toContainText(amount);
    const balances = await api<Balance[]>(
        page,
        "GET",
        `/api/v1/ledgers/${first.ledger.id}/balances`,
    );
    expect(
        Object.fromEntries(
            balances
                .filter((item) => item.account_id === account.id)
                .map((item) => [item.asset_id, item.amount]),
        ),
    ).toEqual(expectedAmounts);
    expect(
        balances
            .filter((item) => item.account_id === account.id)
            .every((item) => item.account_archived),
    ).toBe(true);
    await expectNoOverflow(page);
    await page.screenshot({
        path: testInfo.outputPath("archived-exact-balances.png"),
        fullPage: true,
    });
    await row.getByRole("button", { name: "Restore", exact: true }).click();
    await expect(row.getByRole("button", { name: "Archive", exact: true })).toBeVisible();
});

test("two tabs preserve the stale account draft while the real API rejects its old version", async ({
    page,
    context,
}) => {
    await login(page);
    const entity = await personal(page, fictionalName("conflict"));
    const accountsPath = `/api/v1/ledgers/${entity.ledger.id}/accounts`;
    const account = await api<Account>(page, "POST", accountsPath, {
        name: fictionalName("original account"),
        kind: "bank",
        asset_ids: ["USD"],
    });
    await openLedger(page, entity, "Accounts");
    const second = await context.newPage();
    await second.goto("/");
    await expect(second.getByTestId("current-username")).toHaveText(username);
    await selectLedger(second, entity.name);
    await second.getByRole("button", { name: "Accounts", exact: true }).click();
    for (const tab of [page, second])
        await tab
            .getByTestId(`account-${account.id}`)
            .getByRole("button", { name: "Edit", exact: true })
            .click();
    const acceptedName = fictionalName("accepted edit");
    const staleName = fictionalName("unsaved stale draft");
    await page.getByLabel("Account name", { exact: true }).fill(acceptedName);
    await second.getByLabel("Account name", { exact: true }).fill(staleName);
    const accepted = await save<Account>(page, `${accountsPath}/${account.id}`, "PATCH");
    expect(accepted.version).toBe(2);
    const rejectedPromise = second.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === `${accountsPath}/${account.id}` &&
            response.request().method() === "PATCH",
    );
    await second.getByRole("button", { name: "Save", exact: true }).click();
    const rejected = await rejectedPromise;
    expect(rejected.status()).toBe(409);
    expect((await rejected.json()).detail.code).toBe("version_conflict");
    await expect(second.getByRole("alert")).toContainText(/changed|conflict/i);
    await expect(second.getByLabel("Account name", { exact: true })).toHaveValue(staleName);
    await expect(second.getByRole("button", { name: "Reload", exact: true })).toBeVisible();
    const persisted = (await api<Account[]>(page, "GET", accountsPath)).find(
        (item) => item.id === account.id,
    )!;
    expect(persisted.name).toBe(acceptedName);
    expect(persisted.version).toBe(2);
    await expectNoOverflow(second);
    await second.getByRole("button", { name: "Reload", exact: true }).click();
    await expect(second.getByLabel("Account name", { exact: true })).toHaveValue(acceptedName);
    const recoveredName = fictionalName("explicitly reloaded edit");
    await second.getByLabel("Account name", { exact: true }).fill(recoveredName);
    const recovered = await save<Account>(second, `${accountsPath}/${account.id}`, "PATCH");
    expect(recovered.version).toBe(3);
    expect(recovered.name).toBe(recoveredName);
    await expect(second.getByTestId(`account-${account.id}`)).toContainText(recoveredName);
    await second.close();
});

test("account charts follow the saved type and ledger in both languages and narrow layouts", async ({
    page,
}, testInfo) => {
    await login(page);
    const company = await api<Entity>(page, "POST", "/api/v1/entities", {
        name: fictionalName("Trial design company"),
        kind: "company",
        country_code: "US",
        region_code: "NM",
        company_type: "llc",
        template_key: "business_default",
        locale: "en",
        base_asset_id: "USD",
    });
    const other = await personal(page, fictionalName("Private ledger"));
    const otherBase = `/api/v1/ledgers/${other.ledger.id}`;
    const archived = await api<Account>(page, "POST", `${otherBase}/accounts`, {
        name: fictionalName("Archived bank"),
        kind: "bank",
        asset_ids: ["USD"],
    });
    await api(page, "POST", `${otherBase}/opening-balances`, {
        account_id: archived.id,
        asset_id: "USD",
        amount: "45.67",
        transaction_date: "2026-01-01",
    });
    await api(page, "PATCH", `${otherBase}/accounts/${archived.id}`, {
        expected_version: archived.version,
        archived: true,
    });
    const base = `/api/v1/ledgers/${company.ledger.id}`;
    const fixtures = [
        { kind: "bank", asset: "USD", amount: "80.00", chart: "balance", share: "80%" },
        { kind: "cash", asset: "USD", amount: "20.00", chart: "balance", share: "20%" },
        { kind: "credit_card", asset: "USD", amount: "-140.00", chart: "credit", share: "100%" },
        { kind: "wise", asset: "EUR", amount: "12.34", chart: "payment", share: "100%" },
        {
            kind: "crypto",
            asset: "ETH",
            amount: "123456789.123456789000000001",
            chart: "holding",
            share: "100%",
        },
    ];
    const records = [];
    for (const fixture of fixtures) {
        const { asset, amount } = fixture;
        const accountName =
            fixture.kind === "crypto"
                ? fictionalName("wallet").padEnd(140, "X")
                : fictionalName(fixture.kind);
        const account = await api<Account>(page, "POST", `${base}/accounts`, {
            name: accountName,
            kind: fixture.kind,
            asset_ids: [asset],
        });
        await api(page, "POST", `${base}/opening-balances`, {
            account_id: account.id,
            asset_id: asset,
            amount,
            transaction_date: "2026-01-01",
            description: "Fictional chart opening",
        });
        records.push({ fixture, account });
    }
    await page.goto(`/?entity=${company.id}&view=accounts`);
    const companyMark = page.locator('.ledger-picker .entity-mark[data-kind="company"]');
    await expect(companyMark).toHaveAttribute("aria-hidden", "true");
    const flag = companyMark.locator("img");
    await expect(flag).toHaveAttribute("src", "/assets/icons/flags/us.svg");
    await expect
        .poll(() => flag.evaluate((img: HTMLImageElement) => img.naturalWidth))
        .toBeGreaterThan(0);
    await expect(companyMark).toHaveText("NM");
    for (const { fixture, account } of records) {
        const card = page.getByTestId(`account-${account.id}`);
        await expect(card.locator(`[data-account-kind="${fixture.kind}"]`)).toBeVisible();
        const chart = card.getByRole("img", {
            name: new RegExp(`${fixture.asset}.*${fixture.share}`),
        });
        await expect(chart).toBeVisible();
        await expect(chart).toHaveAttribute("data-chart", fixture.chart);
        await expect(
            card.getByLabel(`${fixture.asset} ${fixture.amount}`, { exact: true }),
        ).toBeVisible();
    }

    const wallet = records.find(({ fixture }) => fixture.kind === "crypto")!.account;
    const widths = testInfo.project.name.startsWith("mobile") ? [375, 320] : [1440];
    for (const width of widths) {
        await page.setViewportSize({ width, height: 850 });
        for (const locale of ["English", "中文"]) {
            await page.getByRole("button", { name: locale, exact: true }).click();
            const card = page.getByTestId(`account-${wallet.id}`);
            await expect(card.getByRole("heading", { name: wallet.name })).toBeVisible();
            await expect(card).toContainText("123,456,789.123456789000000001");
            const edit = card.getByRole("button", {
                name: locale === "English" ? "Edit" : "编辑",
                exact: true,
            });
            await edit.focus();
            await expect(edit).toBeFocused();
            const gap = await page.locator(".ledger-select").evaluate((picker) => {
                const select = picker.querySelector("select")!;
                const textStart =
                    select.getBoundingClientRect().left +
                    parseFloat(getComputedStyle(select).paddingInlineStart);
                return (
                    textStart - picker.querySelector(".entity-mark")!.getBoundingClientRect().right
                );
            });
            expect(gap).toBeGreaterThan(0);
            await expectNoOverflow(page);
        }
    }
    await page.getByRole("button", { name: "English", exact: true }).click();
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    const accountBalances = page.locator(".business-panel").filter({
        has: page.getByRole("heading", { name: "Account balances", exact: true }),
    });
    await expect(
        accountBalances.getByLabel("ETH 123456789.123456789000000001", { exact: true }),
    ).toContainText("123,456,789.123456789000000001");
    await expect(page.getByRole("img", { name: /USD.*card debt.*100%/i })).toBeVisible();
    await expectNoOverflow(page);
    await selectLedger(page, other.name);
    await expect(
        page.locator('.ledger-picker .entity-mark[data-kind="personal"] svg'),
    ).toBeVisible();
    await expect(
        page.getByText("No balances match this selection.", { exact: true }),
    ).toBeVisible();
    await expect(
        accountBalances.getByLabel("ETH 123456789.123456789000000001", { exact: true }),
    ).toHaveCount(0);
    await page.getByLabel("Show archived", { exact: true }).check();
    await expect(accountBalances.getByLabel("USD 45.67", { exact: true })).toBeVisible();
    const navigation = page.getByRole("navigation", { name: "Workspace navigation" });
    if (testInfo.project.name.startsWith("mobile")) {
        const toggle = navigation.getByRole("button", { name: "Show all sections" });
        await toggle.click();
        await expect(navigation.getByRole("button", { name: "Collapse sections" })).toHaveAttribute(
            "aria-expanded",
            "true",
        );
        const daybook = navigation.getByRole("button", { name: "Daybook", exact: true });
        await page.keyboard.press("Tab");
        await expect(daybook).toBeFocused();
        await page.keyboard.press("Space");
        await expect(daybook).toHaveAttribute("aria-expanded", "false");
        await expect(navigation.getByRole("button", { name: "Overview", exact: true })).toHaveCount(
            0,
        );
        await navigation.getByRole("button", { name: "Ledger details", exact: true }).click();
        await expect(navigation.getByRole("button", { name: "Show all sections" })).toHaveAttribute(
            "aria-expanded",
            "false",
        );
        await expect(page.getByRole("heading", { name: other.name, exact: true })).toBeVisible();
        await expectNoOverflow(page);
    } else {
        const daybook = navigation.getByRole("button", { name: "Daybook", exact: true });
        await daybook.click();
        await expect(daybook).toHaveAttribute("aria-expanded", "false");
        await expect(navigation.getByRole("button", { name: "Overview", exact: true })).toHaveCount(
            0,
        );
        const details = navigation.getByRole("button", { name: "Ledger details", exact: true });
        await details.scrollIntoViewIfNeeded();
        await expect(details).toBeInViewport();
        await details.click();
        await expect(page.getByRole("heading", { name: other.name, exact: true })).toBeVisible();
    }
    await page.reload();
    await expect(page.getByTestId("current-username")).toBeAttached();
    if (testInfo.project.name.startsWith("mobile")) {
        await navigation.getByRole("button", { name: "Show all sections" }).click();
    }
    const persistedDaybook = navigation.getByRole("button", { name: "Daybook", exact: true });
    await expect(persistedDaybook).toHaveAttribute("aria-expanded", "false");
    await persistedDaybook.click();
    await expect(navigation.getByRole("button", { name: "Overview", exact: true })).toBeVisible();
});

test("account drawer applies regional fields, protects card data, and preserves other details and assets", async ({
    page,
}) => {
    await login(page);
    const entity = await personal(page, fictionalName("account drawer"));
    await openLedger(page, entity, "Accounts");
    const newAccount = page.getByRole("button", { name: "New account", exact: true });
    await newAccount.click();
    let editor = page.getByRole("dialog", { name: "New account", exact: true });
    await expect(editor.getByRole("heading", { name: "New account" })).toBeFocused();
    await expect(editor).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await page.keyboard.press("Escape");
    await expect(newAccount).toBeFocused();
    await newAccount.click();
    await editor.getByLabel("Account name", { exact: true }).fill("Fictional clearing account");
    await editor.getByRole("radio", { name: "WeChat", exact: true }).check();
    await expect(editor.getByLabel("CNY", { exact: true })).toBeChecked();
    await expect(editor.getByLabel("ETH", { exact: true })).toHaveCount(0);

    await editor.getByRole("radio", { name: "Bank account", exact: true }).check();
    await expect(editor.getByLabel("CNY", { exact: true })).not.toBeChecked();
    await editor.getByLabel("Search banks").fill("CHASUS33");
    await editor.getByLabel("Bank", { exact: true }).selectOption("chase-us");
    await expect(editor.getByLabel("SWIFT / BIC")).toHaveValue("CHASUS33");
    await expect(editor.getByLabel("Bank name")).toHaveValue("Chase");
    await editor.getByLabel("Routing number").fill("000000001");
    await expect(editor.getByLabel("IBAN")).toHaveCount(0);
    await editor.getByLabel("USD", { exact: true }).check();
    const originalWidth = page.viewportSize()!.width;
    if (originalWidth <= 375) {
        await page.setViewportSize({ width: 320, height: 720 });
        expect((await editor.boundingBox())?.width).toBe(320);
    }
    await expectNoOverflow(page);
    await editor.getByRole("button", { name: "中文", exact: true }).click();
    editor = page.getByRole("dialog", { name: "新增账户", exact: true });
    await expect(editor.getByLabel("联行号")).toHaveCount(0);
    await expect(editor.getByLabel("路由号码")).toHaveValue("000000001");
    await expect(editor.getByLabel("账户名称")).toHaveValue("Fictional clearing account");
    await expectNoOverflow(page);
    await editor.getByRole("button", { name: "English", exact: true }).click();
    editor = page.getByRole("dialog", { name: "New account", exact: true });

    const path = `/api/v1/ledgers/${entity.ledger.id}/accounts`;
    const created = await save<Account>(page, path);
    await expect(page.getByRole("status").filter({ hasText: "Account saved." })).toBeVisible();
    const card = page.getByTestId(`account-${created.id}`);
    await expect(card).toHaveAttribute("data-recently-saved", "true");
    expect(created.details).toMatchObject({
        institution: "chase-us",
        bank_name: "Chase",
        routing_number: "000000001",
    });
    await card.getByRole("button", { name: "Edit", exact: true }).click();
    editor = page.getByRole("dialog", { name: "Edit account", exact: true });
    await editor.getByRole("radio", { name: "Credit card", exact: true }).check();
    await expect(editor.getByLabel("Routing number")).toHaveCount(0);
    await editor.getByText("Other saved details", { exact: true }).click();
    await expect(editor.getByText("000000001", { exact: true })).toBeVisible();
    await editor.getByLabel("Last four card digits").fill("1234-1234-1234-1234");
    await editor.getByRole("button", { name: "Save", exact: true }).click();
    await expect(editor.getByRole("alert")).toContainText("exactly four");
    await editor.getByLabel("Last four card digits").fill("0001");
    await editor.getByLabel("Statement day").fill("25");
    const edited = await save<Account>(page, `${path}/${created.id}`, "PATCH");
    expect(edited.kind).toBe("credit_card");
    expect(edited.asset_ids).toEqual(["USD"]);
    expect(edited.details).toMatchObject({
        ...created.details,
        card_last_four: "0001",
        statement_day: "25",
    });

    const { csrf_token } = await (await page.request.get("/api/v1/auth/session")).json();
    const rejected = await page.request.patch(`${path}/${created.id}`, {
        headers: {
            Origin: new URL(page.url()).origin,
            "X-CSRF-Token": csrf_token,
        },
        data: { expected_version: edited.version, details: { card_number: "1234-1234-1234-1234" } },
    });
    expect(rejected.status()).toBe(422);
    expect((await rejected.json()).detail.code).toBe("invalid_card_details");
    const stored = (await api<Account[]>(page, "GET", path)).find((item) => item.id === edited.id);
    expect(stored?.details).toEqual(edited.details);

    await card.getByRole("button", { name: "Edit", exact: true }).click();
    await editor.getByRole("radio", { name: "Crypto wallet", exact: true }).check();
    await expect(editor.getByLabel("ETH", { exact: true })).toBeVisible();
    await expect(editor.getByLabel("USD", { exact: true })).toBeChecked();
    await editor.getByLabel("Network", { exact: true }).selectOption("ethereum");
    await editor.getByLabel("Wallet address").fill("0x123");
    await editor.getByRole("button", { name: "Save", exact: true }).click();
    await expect(editor.getByRole("alert")).toContainText("selected network");
    await editor.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(card.getByText("Credit card", { exact: true })).toBeVisible();
});
