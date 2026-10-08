import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, login, username } from "./helpers";
import { seed } from "./ocr-helpers";
import type { Account, Category, OperationState } from "../src/ledger-api";

test("human correction posts once after lost reply, 401 and same-owner login", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed(),
        base = `/api/v1/ledgers/${fixture.ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: "Fictional OCR bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const category = await api<Category>(page, "POST", `${base}/categories`, {
        name: "Fictional OCR office",
        kind: "expense",
    });
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await page.getByRole("button", { name: /^Draft 1 · Review needed$/u }).click();
    await expect(
        page.getByRole("button", { name: "Confirm and post", exact: true }),
    ).toBeDisabled();
    for (const field of ["Total", "Currency", "Document date"])
        await page.getByLabel(`${field} · Reviewed`, { exact: true }).check();
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
    await page.getByRole("button", { name: "Use reviewed candidates", exact: true }).click();
    await expect(page.getByLabel("Account", { exact: true })).toHaveValue("");
    await expect(page.getByLabel("Amount", { exact: true })).toHaveValue("10.00");
    await page.getByLabel("Account", { exact: true }).selectOption(account.id);
    await page.getByLabel("Amount", { exact: true }).fill("12.50");
    await page.getByLabel("Recognition date", { exact: true }).fill("2031-07-18");
    await page.getByLabel("Category", { exact: true }).selectOption(category.id);
    await page.getByLabel("Split amount", { exact: true }).fill("12.50");
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(
                page.getByLabel(language === "zh" ? "金额" : "Amount", { exact: true }),
            ).toHaveValue("12.50");
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-prepared-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    await expect(page.getByRole("button", { name: "Confirm and post", exact: true })).toBeEnabled();
    expect(await (await page.request.get(`${base}/operations`)).json()).toEqual([]);
    const originals: string[] = [];
    await page.route("**/ocr-drafts/*/confirmations", async (route) => {
        originals.push(route.request().postData()!);
        if (originals.length === 1) {
            const committed = await route.fetch();
            expect(committed.status(), await committed.text()).toBe(201);
            await route.abort("failed");
        } else if (originals.length === 2) {
            const auth = await (await page.request.get("/api/v1/auth/session")).json();
            const revoked = await page.request.post("/api/v1/auth/logout", {
                headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": auth.csrf_token },
            });
            expect(revoked.status()).toBe(204);
            await route.continue(); // Real revoked session: the API returns 401.
        } else await route.continue();
    });
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    const signedIn = page.waitForResponse((response) =>
        response.url().endsWith("/api/v1/auth/login"),
    );
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    expect((await signedIn).status()).toBe(200);
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect(page.getByText(/Document confirmation completed/u)).toBeVisible();
    expect(originals).toHaveLength(3);
    expect(new Set(originals).size).toBe(1);
    const operations = await api<OperationState[]>(page, "GET", `${base}/operations`);
    expect(operations).toHaveLength(1);
    const posting = operations[0].latest_posting;
    expect(posting.kind).toBe("expense");
    if (posting.kind !== "expense") throw new Error("Expected the fictional expense");
    expect(posting.amount).toBe("12.50");
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await page.getByRole("button", { name: /^Draft 1 · Confirmed$/u }).click();
    await expect(page.getByText(/Original confirmation receipt/u)).toContainText("12.50 USD");
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-confirmed-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
});
