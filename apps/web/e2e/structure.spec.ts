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
    await page.getByLabel("Account type", { exact: true }).selectOption("wise");
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
