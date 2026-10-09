import { expect, test, type Page } from "@playwright/test";
import { api, fictionalName, login, username, expectNoOverflow } from "./helpers";
import type { Entity } from "../src/ledger-api";
import type { Reminder } from "../src/reminder-api";

async function setup(page: Page) {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("reminder recovery"),
        base_asset_id: "USD",
        locale: "en",
    });
    const base = `/api/v1/ledgers/${entity.ledger.id}/reminders`;
    await page.goto(`/?entity=${entity.id}&view=reminders`);
    await page.getByRole("button", { name: "New reminder event", exact: true }).click();
    await page.getByLabel("Event kind", { exact: true }).selectOption("tax");
    await page.getByLabel("Event title", { exact: true }).fill("Fictional recovered tax 税务");
    await page.getByLabel("Manual date", { exact: true }).fill("2028-04-01");
    await page
        .getByLabel("Reason for this manual date action", { exact: true })
        .fill("Fictional accountant confirmation");
    return { entity, base };
}
test("fictional reminder unknown creation survives double click, 401 and original-intent retry", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await setup(page);
    const bodies: string[] = [],
        tokens: string[] = [];
    await page.route(`**${data.base}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        bodies.push(route.request().postData()!);
        tokens.push(route.request().headers()["x-csrf-token"]);
        if (bodies.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(201);
            return route.abort("failed");
        }
        if (bodies.length === 2)
            return route.fulfill({
                status: 401,
                contentType: "application/json",
                body: JSON.stringify({ detail: "unauthorized" }),
            });
        return route.continue();
    });
    await page.getByRole("button", { name: "Create event", exact: true }).evaluate((button) => {
        (button as HTMLButtonElement).click();
        (button as HTMLButtonElement).click();
    });
    const retry = page.getByRole("button", { name: "Retry original event request", exact: true });
    await expect(retry).toBeVisible();
    expect(bodies).toHaveLength(1);
    await expect(page.getByRole("button", { name: "Transactions", exact: true })).toBeDisabled();
    await expect(page.getByRole("form", { name: "Reminder event form", exact: true })).toHaveCount(
        0,
    );
    for (const locale of ["zh", "en"]) {
        await page.setViewportSize({ width: 320, height: 1000 });
        await page
            .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
            .click();
        await expect(page.getByTestId("reminder-original-intent")).toContainText(
            "Fictional recovered tax 税务",
        );
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`reminder-unknown-${locale}-320.png`),
            fullPage: true,
        });
    }
    await retry.click();
    await page.getByLabel("Username", { exact: true }).fill(username);
    await expect(page.getByTestId("reminder-original-intent")).toHaveCount(0);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByTestId("reminder-original-intent")).toContainText(
        "Fictional recovered tax 税务",
    );
    await retry.click();
    await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    expect(bodies).toHaveLength(3);
    expect(new Set(bodies).size).toBe(1);
    expect(tokens[2]).not.toBe(tokens[0]);
    await page.unroute(`**${data.base}`);
    const rows = await api<Reminder[]>(page, "GET", data.base);
    expect(rows).toHaveLength(1);
    expect(rows[0].version).toBe(1);
});
test("fictional lost reminder update remains a conflict even when fields match", async ({
    page,
}) => {
    const data = await setup(page);
    await page.getByRole("button", { name: "Create event", exact: true }).click();
    await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    const [row] = await api<Reminder[]>(page, "GET", data.base);
    await page.getByRole("button", { name: "View saved event", exact: true }).click();
    await page.getByRole("button", { name: "Edit details", exact: true }).click();
    await page.getByLabel("Event title", { exact: true }).fill("Fictional unknown update");
    const bodies: string[] = [],
        path = `${data.base}/${row.id}`;
    await page.route(`**${path}`, async (route) => {
        if (route.request().method() !== "PATCH") return route.continue();
        bodies.push(route.request().postData()!);
        if (bodies.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(200);
            return route.abort("failed");
        }
        return route.continue();
    });
    await page.getByRole("button", { name: "Save details", exact: true }).click();
    await page.getByRole("button", { name: "Retry original event request", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("Current data does not prove");
    await expect(page.getByTestId("reminder-original-intent")).toContainText(
        "Fictional unknown update",
    );
    expect(bodies).toHaveLength(2);
    expect(new Set(bodies).size).toBe(1);
    expect((await api<Reminder>(page, "GET", path)).version).toBe(2);
    await page.unroute(`**${path}`);
    await page.getByRole("button", { name: "Reload current event", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Fictional unknown update", exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("reminder-original-intent")).toHaveCount(0);
    await expect(page.getByTestId("reminder-revision")).toHaveCount(2);
});

test("fictional reminder rejection and read failure preserve input and explicit recovery", async ({
    page,
}) => {
    const data = await setup(page);
    await page.route(`**${data.base}`, (route) =>
        route.request().method() === "POST"
            ? route.fulfill({
                  status: 422,
                  contentType: "application/json",
                  body: JSON.stringify({ detail: "Fictional validation failure" }),
              })
            : route.continue(),
    );
    await page.getByRole("button", { name: "Create event", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("Check dates");
    await expect(page.getByLabel("Event title", { exact: true })).toHaveValue(
        "Fictional recovered tax 税务",
    );
    expect(await api(page, "GET", data.base)).toEqual([]);
    await page.unroute(`**${data.base}`);
    await page.getByRole("button", { name: "Create event", exact: true }).click();
    await expect(page.getByText("Event saved and verified.", { exact: true })).toBeVisible();
    const [row] = await api<Reminder[]>(page, "GET", data.base),
        path = `${data.base}/${row.id}`;
    await page.route(`**${path}`, (route) =>
        route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
    );
    await page.getByRole("button", { name: "View saved event", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("could not be completed");
    await expect(page.getByRole("button", { name: "Edit details", exact: true })).toHaveCount(0);
    await page.unroute(`**${path}`);
    await page.getByRole("button", { name: "Retry loading event", exact: true }).click();
    await expect(page.getByRole("button", { name: "Edit details", exact: true })).toBeVisible();
    expect((await api<Reminder>(page, "GET", path)).version).toBe(1);
});

test("fictional reminder logout ignores a late completed write and isolates another ledger", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await setup(page);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route(`**${data.base}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        const response = await route.fetch();
        expect(response.status()).toBe(201);
        await gate;
        await route.fulfill({ response });
    });
    try {
        await page.getByRole("button", { name: "Create event", exact: true }).click();
        await expect
            .poll(async () => (await api<Reminder[]>(page, "GET", data.base)).length)
            .toBe(1);
        await page.getByRole("button", { name: "Sign out", exact: true }).click();
        await page.getByLabel("Username", { exact: true }).fill(username);
        await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
        await page.getByRole("button", { name: "Sign in", exact: true }).click();
        await page.getByRole("button", { name: "Parties and projects", exact: true }).click();
        const delivered = page.waitForResponse(
            (response) =>
                response.url().endsWith(data.base) && response.request().method() === "POST",
        );
        release();
        await delivered;
        await expect(
            page.getByRole("heading", { name: "Parties and projects", exact: true }).first(),
        ).toBeVisible();
        await expect(page.getByTestId("reminder-original-intent")).toHaveCount(0);
        await expect(
            page.getByRole("button", { name: "Retry original event request", exact: true }),
        ).toHaveCount(0);
        const other = await api<Entity>(page, "POST", "/api/v1/entities", {
            kind: "personal",
            name: fictionalName("isolated reminders"),
            base_asset_id: "USD",
            locale: "en",
        });
        await page.goto(`/?entity=${other.id}&view=reminders`);
        await expect(
            page.getByText("This ledger has no reminder events yet.", { exact: true }),
        ).toBeVisible();
        await expect(page.getByText("Fictional recovered tax 税务", { exact: true })).toHaveCount(
            0,
        );
        expect(await api(page, "GET", `/api/v1/ledgers/${other.ledger.id}/reminders`)).toEqual([]);
        expect((await api<Reminder[]>(page, "GET", data.base)).length).toBe(1);
    } finally {
        release();
        await page.unroute(`**${data.base}`);
    }
});
