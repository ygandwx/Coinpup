import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login, username } from "./helpers";
import type { Account, Entity } from "../src/ledger-api";
import type { PeriodReceipt, PeriodState } from "../src/periods-api";

test("fictional period changes preserve intent, require explicit reopening and retain bilingual history", async ({
    page,
}, info) => {
    test.setTimeout(60_000); // Six screenshots plus real logout/login, conflict and reopen transactions.
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("period ledger"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const base = `/api/v1/ledgers/${entity.ledger.id}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: fictionalName("period cash"),
        kind: "cash",
        asset_ids: ["USD"],
    });
    await page.goto(`/?entity=${entity.id}&view=periods`);
    await expect(page.getByTestId("period-version")).toHaveText("1");
    await expect(page.getByTestId("period-cutoff")).toHaveText("Open");
    await page.getByLabel("New cutoff date", { exact: true }).fill("2026-01-31");
    await page.getByLabel("Reason", { exact: true }).fill(" Fictional January close ");
    const originals: { key: string; body: string }[] = [];
    await page.route(`**${base}/period-changes`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        originals.push({
            key: route.request().headers()["idempotency-key"],
            body: route.request().postData()!,
        });
        if (originals.length === 1) {
            const response = await route.fetch();
            expect(response.status(), await response.text()).toBe(201);
            await route.abort("failed");
        } else if (originals.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            const revoked = await page.request.post("/api/v1/auth/logout", {
                headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": auth.csrf_token },
            });
            expect(revoked.status()).toBe(204);
            await route.continue();
        } else await route.continue();
    });
    await page.getByRole("button", { name: "Submit period change", exact: true }).click();
    await expect(page.getByLabel("Reason", { exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Transactions", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Retry original period request", exact: true }).click();
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("button", { name: "Retry original period request", exact: true }).click();
    await expect(page.getByText("Period change completed.", { exact: true })).toBeVisible();
    await expect(page.getByTestId("period-version")).toHaveText("2");
    expect(originals).toHaveLength(3);
    expect(new Set(originals.map((x) => x.body)).size).toBe(1);
    expect(new Set(originals.map((x) => x.key)).size).toBe(1);
    expect(JSON.parse(originals[0].body).reason).toBe(" Fictional January close ");
    expect(await api<PeriodReceipt[]>(page, "GET", `${base}/period-changes`)).toHaveLength(1);
    await page.unroute(`**${base}/period-changes`);

    // A concurrent close must not be silently replaced by a new version/key.
    await api(page, "POST", `${base}/period-changes`, {
        action: "close",
        expected_version: 2,
        closed_through: "2026-02-28",
        reason: "Fictional concurrent close",
    });
    await page.getByLabel("New cutoff date", { exact: true }).fill("2026-03-31");
    await page.getByLabel("Reason", { exact: true }).fill("Fictional stale input kept");
    await page.getByRole("button", { name: "Submit period change", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("period changed elsewhere");
    await expect(
        page.getByRole("button", { name: "Submit period change", exact: true }),
    ).toBeDisabled();
    await page.getByRole("button", { name: "Reload period state", exact: true }).click();
    await expect(page.getByTestId("period-version")).toHaveText("3");
    await expect(page.getByLabel("Reason", { exact: true })).toHaveValue(
        "Fictional stale input kept",
    );
    await expect(page.getByLabel("New cutoff date", { exact: true })).toHaveValue("2026-03-31");

    const opening = {
        account_id: account.id,
        asset_id: "USD",
        amount: "10.00",
        transaction_date: "2026-01-20",
        description: "Fictional reopened opening",
    };
    const auth = await (await page.request.get("/api/v1/auth/session")).json();
    const rejected = await page.request.post(`${base}/opening-balances`, {
        data: opening,
        headers: {
            Origin: new URL(page.url()).origin,
            "X-CSRF-Token": auth.csrf_token,
            "Idempotency-Key": randomUUID(),
        },
    });
    expect(rejected.status()).toBe(409);
    expect((await rejected.json()).detail.code).toBe("period_closed");
    expect(await api(page, "GET", `${base}/operations`)).toEqual([]);
    await page.getByLabel("Action", { exact: true }).selectOption("reopen");
    await page.getByLabel("New cutoff date", { exact: true }).fill("");
    await page.getByLabel("Reason", { exact: true }).fill("Fictional correction approved");
    await page.getByRole("button", { name: "Submit period change", exact: true }).click();
    await expect(page.getByTestId("period-version")).toHaveText("4");
    await expect(page.getByTestId("period-cutoff")).toHaveText("Open");
    await api(page, "POST", `${base}/opening-balances`, opening);
    await page.getByLabel("Action", { exact: true }).selectOption("close");
    await page.getByLabel("New cutoff date", { exact: true }).fill("2026-03-31");
    await page.getByLabel("Reason", { exact: true }).fill("Fictional correction completed");
    await page.getByRole("button", { name: "Submit period change", exact: true }).click();
    await expect(page.getByTestId("period-version")).toHaveText("5");
    await expect(page.getByTestId("period-audit")).toHaveCount(4);
    const before = await api<PeriodState>(page, "GET", `${base}/period`);
    expect(before.last_reopened?.reason).toBe("Fictional correction approved");
    const history = await api(page, "GET", `${base}/period-changes`);
    const operations = await api<unknown[]>(page, "GET", `${base}/operations`);
    expect(operations).toHaveLength(1);
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const locale of ["zh", "en"]) {
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(
                page.getByLabel(locale === "zh" ? "理由" : "Reason", { exact: true }),
            ).toHaveValue("Fictional correction completed");
            await expect(
                page.getByLabel(locale === "zh" ? "新的截止日" : "New cutoff date", {
                    exact: true,
                }),
            ).toHaveValue("2026-03-31");
            await expect(page.getByTestId("period-version")).toHaveText("5");
            await expect(page.getByTestId("period-audit")).toHaveCount(4);
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`period-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    }
    expect(await api(page, "GET", `${base}/period-changes`)).toEqual(history);
    expect(await api(page, "GET", `${base}/operations`)).toEqual(operations);
    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await expect(page.getByRole("button", { name: "New entry", exact: true })).toBeVisible();
    await expect(page.getByTestId("period-version")).toHaveCount(0);
});
