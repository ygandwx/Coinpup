import { expect, test } from "@playwright/test";
import { api, username, fictionalName, expectNoOverflow } from "./helpers";
import { fillRecurring, recurringSetup, runRecurringJob } from "./recurring-helpers";
import type { RecurringRule, RecurringInstance } from "../src/recurring-api";
import type { Entity } from "../src/ledger-api";
import { draftFields } from "../src/draft-fields";

test("fictional recurring failures preserve selection and require refreshing a stale source version", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await recurringSetup(page, "2099-01-01");
    await fillRecurring(page, "Fictional retained selection", data.date);
    await page.getByLabel("Rule timezone", { exact: true }).fill("Fictional/Invalid");
    await page.getByRole("button", { name: "Save recurring rule", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("Enter a valid IANA timezone");
    await expect(page.getByLabel("Rule name", { exact: true })).toHaveValue(
        "Fictional retained selection",
    );
    await expect(page.getByTestId("recurring-source-selected")).toContainText("Version 1");
    expect(await api(page, "GET", data.base)).toEqual([]);
    await api(page, "PUT", `${data.ledger}/business-documents/${data.source.id}`, {
        ...draftFields(data.source),
        expected_version: 1,
        notes: "Fictional explicitly refreshed template",
    });
    await page.getByLabel("Rule timezone", { exact: true }).fill("Asia/Shanghai");
    await page.getByRole("button", { name: "Save recurring rule", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Save recurring rule", exact: true }),
    ).toBeEnabled();
    await expect(page.getByTestId("recurring-source-selected")).toContainText("Version 1");
    expect(await api(page, "GET", data.base)).toEqual([]);
    await page.getByRole("button", { name: "Refresh template list", exact: true }).click();
    await expect(
        page.getByRole("region", { name: "Choose template draft", exact: true }),
    ).toContainText("Version 2");
    await page.getByTestId("recurring-source").click();
    await page.getByRole("button", { name: "Save recurring rule", exact: true }).click();
    await expect(page.getByText("Rule saved and verified.", { exact: true })).toBeVisible();
    const rules = await api<RecurringRule[]>(page, "GET", data.base);
    expect(rules).toHaveLength(1);
    expect(rules[0]).toMatchObject({
        source_version: 2,
        template_input: { notes: "Fictional explicitly refreshed template" },
    });
    await page.getByRole("button", { name: "Back to rules", exact: true }).click();
    await page.getByRole("button", { name: "View recurring rule", exact: true }).click();
    const archives: string[] = [];
    const archivePath = `${data.base}/${rules[0].id}/archive`;
    await page.route(`**${archivePath}`, async (route) => {
        if (route.request().method() !== "PATCH") return route.continue();
        archives.push(route.request().postData()!);
        if (archives.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(200);
            return route.abort("failed");
        }
        await route.continue();
    });
    await page.getByRole("button", { name: "Pause rule", exact: true }).click();
    await page.getByRole("button", { name: "Retry original rule request", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("The rule changed");
    expect(archives).toHaveLength(2);
    expect(new Set(archives).size).toBe(1);
    await page.getByRole("button", { name: "Reload current rule", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Save recurring rule", exact: true }),
    ).toHaveCount(0);
    await page.unroute(`**${archivePath}`);
    await page.getByRole("button", { name: "Resume rule and catch up", exact: true }).click();
    await expect(page.getByText("Rule saved and verified.", { exact: true })).toBeVisible();
    expect(await api(page, "GET", `${data.base}/${rules[0].id}`)).toMatchObject({
        version: 3,
        archived: false,
        next_index: 0,
        source_version: 2,
    });
    await api(page, "PUT", `${data.ledger}/business-documents/${data.source.id}`, {
        ...draftFields(data.source),
        expected_version: 2,
        notes: "Fictional replacement template v3",
    });
    await page.getByRole("button", { name: "Back to rules", exact: true }).click();
    await page.getByRole("button", { name: "View recurring rule", exact: true }).click();
    await page.getByLabel("Rule name", { exact: true }).fill("Fictional explicitly updated rule");
    await page
        .getByRole("checkbox", { name: "Explicitly refresh captured template", exact: true })
        .check();
    await expect(
        page.getByRole("region", { name: "Choose template draft", exact: true }),
    ).toContainText("Version 3");
    await page.getByTestId("recurring-source").click();
    await page.getByRole("button", { name: "Save recurring rule", exact: true }).click();
    await expect(page.getByText("Rule saved and verified.", { exact: true })).toBeVisible();
    expect(await api(page, "GET", `${data.base}/${rules[0].id}`)).toMatchObject({
        version: 4,
        name: "Fictional explicitly updated rule",
        anchor_date: data.date,
        source_version: 3,
        template_input: { notes: "Fictional replacement template v3" },
    });
    expect(await api(page, "GET", `${data.ledger}/operations`)).toEqual([]);
});

test("fictional recurring original intent survives lost response, 401 and failed workspace reads", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await recurringSetup(page, "2099-01-01");
    await fillRecurring(page, "Fictional recovered recurrence", data.date);
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
        if (bodies.length === 2) {
            const session = await (await page.request.get("/api/v1/auth/session")).json();
            const response = await page.request.post("/api/v1/auth/logout", {
                headers: {
                    Origin: new URL(page.url()).origin,
                    "X-CSRF-Token": session.csrf_token,
                },
            });
            expect(response.status()).toBe(204);
        }
        await route.continue();
    });
    await page
        .getByRole("button", { name: "Save recurring rule", exact: true })
        .evaluate((button) => {
            (button as HTMLButtonElement).click();
            (button as HTMLButtonElement).click();
        });
    const retry = page.getByRole("button", { name: "Retry original rule request", exact: true });
    await expect(retry).toBeVisible();
    expect(bodies).toHaveLength(1);
    await expect(page.getByTestId("recurring-original-intent")).toContainText(
        "Fictional recovered recurrence",
    );
    for (const locale of ["zh", "en"]) {
        await page
            .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
            .click();
        await expect(
            page.getByRole("button", {
                name: locale === "zh" ? "重试原规则请求" : "Retry original rule request",
                exact: true,
            }),
        ).toBeVisible();
        await expect(page.getByTestId("recurring-original-intent")).toContainText(
            "Fictional recovered recurrence",
        );
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`recurring-unknown-${locale}.png`),
            fullPage: true,
        });
    }
    await expect(page.getByRole("button", { name: "Transactions", exact: true })).toBeDisabled();
    await expect(page.getByRole("form", { name: "Recurring rule form", exact: true })).toHaveCount(
        0,
    );
    await retry.click();
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.route("**/api/v1/entities?*", (route) =>
        route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
    );
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByRole("button", { name: "Retry loading", exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Create your first ledger", exact: true }),
    ).toBeDisabled();
    await page.unroute("**/api/v1/entities?*");
    await page.getByRole("button", { name: "Retry loading", exact: true }).click();
    await expect(page.getByTestId("recurring-original-intent")).toContainText(
        "Fictional recovered recurrence",
    );
    await retry.click();
    await expect(page.getByText("Rule saved and verified.", { exact: true })).toBeVisible();
    expect(bodies).toHaveLength(3);
    expect(new Set(bodies).size).toBe(1);
    expect(tokens[2]).not.toBe(tokens[0]);
    await page.unroute(`**${data.base}`);
    const rules = await api<RecurringRule[]>(page, "GET", data.base);
    expect(rules).toHaveLength(1);
    expect(rules[0]).toMatchObject({ version: 1, next_index: 0, anchor_date: data.date });
    expect(await api(page, "GET", `${data.ledger}/operations`)).toEqual([]);
});

test("fictional recurring logout isolates a late completed write and another ledger", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await recurringSetup(page, "2099-01-01");
    await fillRecurring(page, "Fictional delayed private rule", data.date);
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
        await page.getByRole("button", { name: "Save recurring rule", exact: true }).click();
        await expect
            .poll(async () => (await api<RecurringRule[]>(page, "GET", data.base)).length)
            .toBe(1);
        await page.getByRole("button", { name: "Sign out", exact: true }).click();
        await expect(
            page.getByRole("form", { name: "Recurring rule form", exact: true }),
        ).toHaveCount(0);
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
        await expect(page.getByTestId("recurring-original-intent")).toHaveCount(0);
        await expect(
            page.getByRole("button", { name: "Retry original rule request", exact: true }),
        ).toHaveCount(0);
        const other = await api<Entity>(page, "POST", "/api/v1/entities", {
            kind: "personal",
            name: fictionalName("isolated recurring"),
            base_asset_id: "USD",
            template_key: "personal_default",
            locale: "en",
        });
        await page.goto(`/?entity=${other.id}&view=recurring`);
        await expect(
            page.getByText("No recurring rules on this page.", { exact: true }),
        ).toBeVisible();
        await expect(page.getByText("Fictional delayed private rule", { exact: true })).toHaveCount(
            0,
        );
        await page.goto(`/?entity=${other.id}&view=drafts&draft=${data.source.id}`);
        await expect(page.locator(".draft-panel [role=alert]")).toContainText(
            "This record was not found",
        );
        await expect(
            page.getByText("Fictional captured notes 原模板", { exact: true }),
        ).toHaveCount(0);
        await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
        await expect.poll(() => new URL(page.url()).searchParams.has("draft")).toBe(false);
        const rules = await api<RecurringRule[]>(page, "GET", data.base);
        expect(rules).toHaveLength(1);
        expect(rules[0]).toMatchObject({
            version: 1,
            next_index: 0,
            name: "Fictional delayed private rule",
        });
        expect(await api(page, "GET", `${data.ledger}/operations`)).toEqual([]);
    } finally {
        release();
        await page.unroute(`**${data.base}`);
    }
});

test("fictional recurring generation turns an uncertain create into an explicit version conflict", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await recurringSetup(page);
    await fillRecurring(page, "Fictional generation conflict", data.date);
    const bodies: string[] = [];
    await page.route(`**${data.base}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        bodies.push(route.request().postData()!);
        if (bodies.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(201);
            runRecurringJob();
            return route.abort("failed");
        }
        await route.continue();
    });
    await page.getByRole("button", { name: "Save recurring rule", exact: true }).click();
    await page.getByRole("button", { name: "Retry original rule request", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("The rule changed");
    expect(bodies).toHaveLength(2);
    expect(new Set(bodies).size).toBe(1);
    await expect(page.getByTestId("recurring-original-intent")).toContainText(
        "Fictional generation conflict",
    );
    await expect(
        page.getByRole("button", { name: "Save recurring rule", exact: true }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Reload current rule", exact: true }).click();
    await expect(page.getByLabel("Rule name", { exact: true })).toHaveValue(
        "Fictional generation conflict",
    );
    await expect(page.getByLabel("First occurrence date", { exact: true })).toBeDisabled();
    await expect(page.getByTestId("recurring-instance")).toHaveCount(1);
    await page.unroute(`**${data.base}`);
    const rules = await api<RecurringRule[]>(page, "GET", data.base);
    expect(rules).toHaveLength(1);
    expect(rules[0]).toMatchObject({ version: 2, next_index: 1 });
    const instances = await api<RecurringInstance[]>(
        page,
        "GET",
        `${data.base}/${rules[0].id}/instances`,
    );
    expect(instances).toHaveLength(1);
    expect(await api(page, "GET", `${data.ledger}/operations`)).toEqual([]);
});
