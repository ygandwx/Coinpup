import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, fictionalName, login, username } from "./helpers";
import type { Entity, Category } from "../src/ledger-api";
import type { Party } from "../src/business-api";
import type { BusinessDraft, BusinessDraftSummary } from "../src/business-draft-api";
import { draftFields } from "../src/draft-fields";

test("fictional draft retries original create across 401, freezes duplicate clicks and resolves lost update explicitly", async ({
    page,
}) => {
    test.setTimeout(90_000);
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("draft recovery"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const ledger = `/api/v1/ledgers/${entity.ledger.id}`,
        base = `${ledger}/business-documents`;
    const party = await api<Party>(page, "POST", `${ledger}/business-parties`, {
        id: randomUUID(),
        name: "Fictional recovery customer",
        role: "customer",
    });
    const categories = await api<Category[]>(page, "GET", `${ledger}/categories?limit=100`);
    await page.goto(`/?entity=${entity.id}&view=drafts`);
    await page.getByRole("button", { name: "New business draft", exact: true }).click();
    await page.getByLabel("Business party", { exact: true }).selectOption(party.id);
    await page.getByLabel("Pricing asset", { exact: true }).selectOption("USD");
    await page.getByRole("button", { name: "Add line", exact: true }).click();
    await page.getByLabel("Description", { exact: true }).fill("Fictional uninterrupted original");
    await page.getByLabel("Quantity", { exact: true }).fill("2.00");
    await page.getByLabel("Unit price before tax", { exact: true }).fill("10.00");
    await page
        .getByLabel("Category", { exact: true })
        .selectOption(categories.find((row) => row.kind === "income")!.id);
    const creates: string[] = [];
    await page.route(`**${base}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        creates.push(route.request().postData()!);
        if (creates.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(201);
            return route.abort("failed");
        }
        if (creates.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            const logout = await page.request.post("/api/v1/auth/logout", {
                headers: {
                    Origin: new URL(page.url()).origin,
                    "X-CSRF-Token": auth.csrf_token,
                },
            });
            expect(logout.status()).toBe(204);
        }
        await route.continue();
    });
    await page.getByRole("button", { name: "Save draft", exact: true }).evaluate((button) => {
        (button as HTMLButtonElement).click();
        (button as HTMLButtonElement).click();
    });
    await expect(
        page.getByRole("button", { name: "Retry original request", exact: true }),
    ).toBeVisible();
    expect(creates).toHaveLength(1);
    await expect(page.getByLabel("Quantity", { exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Transactions", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(page.getByRole("form", { name: "Business draft form", exact: true })).toHaveCount(
        0,
    );
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.route("**/api/v1/entities?*", (route) =>
        route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
    );
    await page.route(`**${ledger}/categories?*`, (route) =>
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
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(page.getByText("Draft saved and verified.", { exact: true })).toBeVisible();
    expect(creates).toHaveLength(3);
    await expect(page.locator(".draft-panel [role=alert]")).toHaveCount(0);
    expect(new Set(creates).size).toBe(1);
    await page.unroute(`**${base}`);
    const rows = await api<BusinessDraftSummary[]>(page, "GET", base);
    expect(rows).toHaveLength(1);
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    await page.unroute(`**${ledger}/categories?*`);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await page.getByLabel("Quantity", { exact: true }).fill("3.00");
    const updates: string[] = [];
    await page.route(`**${base}/${rows[0].id}`, async (route) => {
        if (route.request().method() !== "PUT") return route.continue();
        updates.push(route.request().postData()!);
        if (updates.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(200);
            return route.abort("failed");
        }
        await route.continue();
    });
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("original edit is not confirmed");
    expect(updates).toHaveLength(2);
    expect(new Set(updates).size).toBe(1);
    await expect(page.getByLabel("Quantity", { exact: true })).toHaveValue("3.00");
    await expect(page.getByRole("button", { name: "Save draft", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Load current draft", exact: true }).click();
    await expect(page.getByLabel("Quantity", { exact: true })).toHaveValue("3.00");
    await expect(page.getByRole("button", { name: "Save draft", exact: true })).toBeEnabled();
    await page.unroute(`**${base}/${rows[0].id}`);
    const current = await api<BusinessDraft>(page, "GET", `${base}/${rows[0].id}`);
    expect(current.version).toBe(2);
    const concurrent = {
        ...draftFields(current),
        notes: "Fictional concurrent saved note",
        expected_version: 2,
    };
    await api(page, "PUT", `${base}/${rows[0].id}`, concurrent);
    await page.getByLabel("Notes", { exact: true }).fill("Fictional preserved unsaved note");
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("original edit is not confirmed");
    await expect(page.getByLabel("Notes", { exact: true })).toHaveValue(
        "Fictional preserved unsaved note",
    );
    await page.getByRole("button", { name: "Load current draft", exact: true }).click();
    await expect(page.getByLabel("Notes", { exact: true })).toHaveValue(
        "Fictional concurrent saved note",
    );
    expect(await api(page, "GET", `${ledger}/operations`)).toEqual([]);
});

test("fictional archive recovery, ledger isolation and logout ignore a delayed completed write", async ({
    page,
}) => {
    test.setTimeout(90_000);
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const createEntity = () =>
        api<Entity>(page, "POST", "/api/v1/entities", {
            kind: "personal",
            name: fictionalName("draft isolated recovery"),
            base_asset_id: "USD",
            template_key: "personal_default",
            locale: "en",
        });
    const entity = await createEntity(),
        other = await createEntity();
    const ledger = `/api/v1/ledgers/${entity.ledger.id}`,
        base = `${ledger}/business-documents`;
    const party = await api<Party>(page, "POST", `${ledger}/business-parties`, {
        id: randomUUID(),
        name: "Fictional isolated draft customer",
        role: "customer",
    });
    const record = await api<BusinessDraft>(page, "POST", base, {
        id: randomUUID(),
        document_kind: "invoice",
        party_id: party.id,
        asset_id: "USD",
        issue_date: "2026-01-01",
        lines: [],
    });
    await page.goto(`/?entity=${other.id}&view=drafts`);
    await expect(page.getByText("No drafts on this page.", { exact: true })).toBeVisible();
    await expect(page.getByText(party.name!, { exact: false })).toHaveCount(0);
    await page.goto(`/?entity=${entity.id}&view=drafts`);
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    const archives: string[] = [];
    await page.route(`**${base}/${record.id}/archive`, async (route) => {
        if (route.request().method() !== "PATCH") return route.continue();
        archives.push(route.request().postData()!);
        if (archives.length === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(200);
            return route.abort("failed");
        }
        await route.continue();
    });
    await page.getByRole("button", { name: "Archive draft", exact: true }).click();
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("original edit is not confirmed");
    expect(archives).toHaveLength(2);
    expect(new Set(archives).size).toBe(1);
    await page.getByRole("button", { name: "Load current draft", exact: true }).click();
    await expect(page.getByRole("button", { name: "Save draft", exact: true })).toBeDisabled();
    await page.unroute(`**${base}/${record.id}/archive`);
    await page.getByRole("button", { name: "Restore draft", exact: true }).click();
    await page.getByRole("button", { name: "Back to drafts", exact: true }).click();
    const restored = await api<BusinessDraft>(page, "GET", `${base}/${record.id}`);
    expect(restored).toMatchObject({ version: 3, archived: false });
    await page.getByRole("button", { name: "View draft", exact: true }).click();
    await page.getByLabel("Notes", { exact: true }).fill("Fictional delayed private note");
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route(`**${base}/${record.id}`, async (route) => {
        if (route.request().method() !== "PUT") return route.continue();
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        await gate;
        await route.fulfill({ response });
    });
    try {
        await page.getByRole("button", { name: "Save draft", exact: true }).click();
        await expect
            .poll(
                async () => (await api<BusinessDraft>(page, "GET", `${base}/${record.id}`)).version,
            )
            .toBe(4);
        await page.getByRole("button", { name: "Sign out", exact: true }).click();
        await expect(
            page.getByRole("form", { name: "Business draft form", exact: true }),
        ).toHaveCount(0);
        await page.getByLabel("Username", { exact: true }).fill(username);
        await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
        await page.getByRole("button", { name: "Sign in", exact: true }).click();
        await page.getByRole("button", { name: "Parties and projects", exact: true }).click();
        const delivered = page.waitForResponse(
            (response) =>
                response.url().endsWith(`${base}/${record.id}`) &&
                response.request().method() === "PUT",
        );
        release();
        await delivered;
        await expect(
            page.getByRole("heading", { name: "Parties and projects", exact: true }).first(),
        ).toBeVisible();
        await expect(
            page.getByRole("form", { name: "Business draft form", exact: true }),
        ).toHaveCount(0);
        await expect(
            page.getByRole("button", { name: "Retry original request", exact: true }),
        ).toHaveCount(0);
        expect(await api<BusinessDraft>(page, "GET", `${base}/${record.id}`)).toMatchObject({
            version: 4,
            notes: "Fictional delayed private note",
        });
        expect(await api(page, "GET", `${ledger}/operations`)).toEqual([]);
    } finally {
        release();
        await page.unroute(`**${base}/${record.id}`);
    }
});
