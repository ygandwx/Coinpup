import { createHash, randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, login, username } from "./helpers";
import { seed } from "./ocr-helpers";
import type { Account, Category, Entity, OperationState } from "../src/ledger-api";
import type { ConfirmationReceipt, DraftDetail } from "../src/ocr-api";

test("destination copy resumes the original upload after lost reply and login, then posts only there", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const source = seed();
    const target = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: `Fictional copy destination ${randomUUID()}`,
        base_asset_id: "USD",
    });
    const base = `/api/v1/ledgers/${target.ledger.id}`,
        sourceBase = `/api/v1/ledgers/${source.ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: "Fictional target bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const category = await api<Category>(page, "POST", `${base}/categories`, {
        name: "Fictional target office",
        kind: "expense",
    });
    const oldAccount = await api<Account>(page, "POST", `${sourceBase}/accounts`, {
        name: "Fictional source bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const oldCategory = await api<Category>(page, "POST", `${sourceBase}/categories`, {
        name: "Fictional source office",
        kind: "expense",
    });
    await page.goto(`/?entity=${source.entity}&view=ocr`);
    await page.getByRole("button", { name: /^Draft 1 · Review needed$/u }).click();
    for (const field of ["Total", "Currency", "Document date"])
        await page.getByLabel(`${field} · Reviewed`, { exact: true }).check();
    await page.getByRole("button", { name: "Save review", exact: true }).click();
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
    await page.getByRole("button", { name: "Use reviewed candidates", exact: true }).click();
    await page.getByLabel("Account", { exact: true }).selectOption(oldAccount.id);
    await page.getByLabel("Recognition date", { exact: true }).fill("2031-07-18");
    await page.getByLabel("Category", { exact: true }).selectOption(oldCategory.id);
    await page.getByLabel("Split amount", { exact: true }).fill("10.00");
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    await page
        .getByLabel("Choose destination ledger", { exact: true })
        .selectOption(target.ledger.id);
    const originals: { url: string; hash: string }[] = [];
    await page.route("**/uploads/*/content", async (route) => {
        originals.push({
            url: route.request().url(),
            hash: createHash("sha256").update(route.request().postDataBuffer()!).digest("hex"),
        });
        if (originals.length === 1) {
            const result = await route.fetch();
            expect(result.status()).toBe(200);
            await route.abort("failed");
        } else if (originals.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            expect(
                (
                    await page.request.post("/api/v1/auth/logout", {
                        headers: {
                            Origin: new URL(page.url()).origin,
                            "X-CSRF-Token": auth.csrf_token,
                        },
                    })
                ).status(),
            ).toBe(204);
            await route.continue();
        } else await route.continue();
    });
    await page.getByRole("button", { name: "Copy original to destination", exact: true }).click();
    await page.getByRole("button", { name: "Retry original copy", exact: true }).click();
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.getByRole("button", { name: "Retry original copy", exact: true }).click();
    await expect(page.getByText(/Original copied\. Return/u)).toBeVisible();
    expect(originals).toHaveLength(3);
    expect(new Set(originals.map((item) => item.url)).size).toBe(1);
    expect(new Set(originals.map((item) => item.hash)).size).toBe(1);
    await page.getByRole("button", { name: "Return to source draft", exact: true }).click();
    await page
        .getByRole("button", {
            name: "Apply copied original and clear prepared entry",
            exact: true,
        })
        .click();
    await expect(
        page.getByRole("region", { name: "Original copy status", exact: true }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("region", { name: "Document destination", exact: true }),
    ).toContainText(target.name);
    expect(await api(page, "GET", `${base}/operations`)).toEqual([]);
    expect(await api(page, "GET", `${sourceBase}/operations`)).toEqual([]);
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
    await page.getByRole("button", { name: "Use reviewed candidates", exact: true }).click();
    await expect(page.getByLabel("Account", { exact: true })).toHaveValue("");
    await expect(
        page.getByLabel("Account", { exact: true }).locator(`option[value="${oldAccount.id}"]`),
    ).toHaveCount(0);
    await page.getByLabel("Account", { exact: true }).selectOption(account.id);
    await page.getByLabel("Recognition date", { exact: true }).fill("2031-07-18");
    await page.getByLabel("Category", { exact: true }).selectOption(category.id);
    await page.getByLabel("Split amount", { exact: true }).fill("10.00");
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-destination-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    await expect(page.getByText(/Document confirmation completed/u)).toBeVisible();
    expect(await api(page, "GET", `${sourceBase}/operations`)).toEqual([]);
    const operations = await api<OperationState[]>(page, "GET", `${base}/operations`);
    expect(operations).toHaveLength(1);
    const receipt = await api<ConfirmationReceipt>(
        page,
        "GET",
        `${sourceBase}/ocr-drafts/${source.draft}/confirmation`,
    );
    const detail = await api<DraftDetail>(page, "GET", `${sourceBase}/ocr-drafts/${source.draft}`);
    expect(receipt.target_ledger_id).toBe(target.ledger.id);
    expect(receipt.target_file_id).not.toBe(detail.file_id);
    const original = await page.request.get(`${sourceBase}/files/${detail.file_id}/content`);
    const copied = await page.request.get(`${base}/files/${receipt.target_file_id}/content`);
    expect(original.status()).toBe(200);
    expect(copied.status()).toBe(200);
    expect(await copied.body()).toEqual(await original.body());
});
