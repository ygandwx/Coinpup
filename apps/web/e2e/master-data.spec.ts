import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login, username } from "./helpers";
import type { Entity } from "../src/ledger-api";
import type { Party, Project } from "../src/business-api";

test("fictional master records preserve interrupted intents and explicitly resolve concurrent edits", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("master ledger"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const base = `/api/v1/ledgers/${entity.ledger.id}/business-parties`;
    await page.goto(`/?entity=${entity.id}&view=masters`);
    await page.getByRole("button", { name: "New record", exact: true }).click();
    const name = fictionalName("客户 supplier");
    await page.getByLabel("Name", { exact: true }).fill(name);
    await page.getByLabel("Party role", { exact: true }).selectOption("supplier");
    await page.getByLabel("Legal name", { exact: true }).fill(" Fictional Paper Company ");
    await page.getByLabel("Notes", { exact: true }).fill("  Fictional 中文 notes  ");
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const locale of ["zh", "en"]) {
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(
                page.getByLabel(locale === "zh" ? "名称" : "Name", { exact: true }),
            ).toHaveValue(name);
            await expect(
                page.getByLabel(locale === "zh" ? "备注" : "Notes", { exact: true }),
            ).toHaveValue("  Fictional 中文 notes  ");
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`masters-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    }
    const originals: string[] = [];
    await page.route(`**${base}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        originals.push(route.request().postData()!);
        if (originals.length === 1) {
            const response = await route.fetch();
            expect(response.status(), await response.text()).toBe(201);
            await route.abort("failed");
        } else if (originals.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            const logout = await page.request.post("/api/v1/auth/logout", {
                headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": auth.csrf_token },
            });
            expect(logout.status()).toBe(204);
            await route.continue();
        } else await route.continue();
    });
    await page.getByRole("button", { name: "Save record", exact: true }).evaluate((button) => {
        (button as HTMLButtonElement).click();
        (button as HTMLButtonElement).click();
    });
    await expect(
        page.getByRole("button", { name: "Retry original request", exact: true }),
    ).toBeVisible();
    expect(originals).toHaveLength(1);
    await expect(page.getByLabel("Name", { exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Transactions", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("button", { name: "Retry original request", exact: true }).click();
    await expect(page.getByText("Record saved and verified.", { exact: true })).toBeVisible();
    expect(originals).toHaveLength(3);
    expect(new Set(originals).size).toBe(1);
    await page.unroute(`**${base}`);
    const rows = await api<Party[]>(page, "GET", base);
    expect(rows).toHaveLength(1);
    expect(rows[0].notes).toBe("  Fictional 中文 notes  ");
    await page.getByRole("button", { name: "Back to records", exact: true }).click();
    await page.getByRole("button", { name: "View and edit", exact: true }).click();
    await api(page, "PATCH", `${base}/${rows[0].id}`, {
        expected_version: 1,
        name: "Fictional concurrent name",
    });
    await page.getByLabel("Notes", { exact: true }).fill("Fictional preserved unsaved draft");
    await page.getByRole("button", { name: "Save record", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("original edit is not confirmed");
    await expect(page.getByLabel("Notes", { exact: true })).toHaveValue(
        "Fictional preserved unsaved draft",
    );
    await expect(page.getByRole("button", { name: "Save record", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Load current record", exact: true }).click();
    await expect(page.getByLabel("Name", { exact: true })).toHaveValue("Fictional concurrent name");
    await page.getByLabel("Archived", { exact: true }).check();
    await page.getByRole("button", { name: "Save record", exact: true }).click();
    await page.getByRole("button", { name: "Back to records", exact: true }).click();
    await page.getByRole("button", { name: "View and edit", exact: true }).click();
    await page.getByLabel("Archived", { exact: true }).uncheck();
    await page.getByRole("button", { name: "Save record", exact: true }).click();
    await page.getByRole("button", { name: "Back to records", exact: true }).click();
    expect((await api<Party[]>(page, "GET", base))[0]).toMatchObject({
        version: 4,
        archived: false,
    });
    await page.getByLabel("Record type", { exact: true }).selectOption("projects");
    await page.getByRole("button", { name: "New record", exact: true }).click();
    await page.getByLabel("Name", { exact: true }).fill("Fictional project 中文");
    await page.getByRole("button", { name: "Save record", exact: true }).click();
    await page.getByRole("button", { name: "Back to records", exact: true }).click();
    expect(
        await api<Project[]>(page, "GET", `/api/v1/ledgers/${entity.ledger.id}/business-projects`),
    ).toHaveLength(1);
});

test("fictional master validation, read failures, ledger isolation and archived inspection", async ({
    page,
}) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const create = () =>
        api<Entity>(page, "POST", "/api/v1/entities", {
            kind: "personal",
            name: fictionalName("isolated master"),
            base_asset_id: "USD",
            template_key: "personal_default",
            locale: "en",
        });
    const first = await create(),
        second = await create();
    await page.goto(`/?entity=${first.id}&view=masters`);
    await page.getByRole("button", { name: "New record", exact: true }).click();
    await page.getByLabel("Name", { exact: true }).fill("   ");
    await page.getByRole("button", { name: "Save record", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("nonempty name");
    await page.getByLabel("Name", { exact: true }).fill("Fictional isolated party");
    await page.getByLabel("Legal name", { exact: true }).fill("   ");
    await page.getByRole("button", { name: "Save record", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("required fields");
    await page.getByLabel("Legal name", { exact: true }).fill("Fictional inspect legal");
    await page.getByRole("button", { name: "Save record", exact: true }).click();
    await page.getByRole("button", { name: "Back to records", exact: true }).click();
    await page.goto(`/?entity=${second.id}&view=masters`);
    await expect(page.getByText("No records on this page.", { exact: true })).toBeVisible();
    await expect(page.getByText("Fictional isolated party", { exact: true })).toHaveCount(0);
    await api(page, "PATCH", `/api/v1/entities/${first.id}`, {
        expected_version: first.version,
        archived: true,
    });
    await page.goto(`/?entity=${first.id}&view=masters`);
    await expect(page.getByRole("button", { name: "New record", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "View record", exact: true }).click();
    await expect(page.getByLabel("Legal name", { exact: true })).toHaveValue(
        "Fictional inspect legal",
    );
    await expect(page.getByRole("button", { name: "Save record", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Finish editing", exact: true }).click();
    await page.route(`**/business-parties?*`, (route) =>
        route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
    );
    await page.getByRole("button", { name: "Refresh records", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await page.unroute(`**/business-parties?*`);
    await page.getByRole("button", { name: "Refresh records", exact: true }).click();
    await expect(page.getByText("Fictional isolated party", { exact: true })).toBeVisible();
});
