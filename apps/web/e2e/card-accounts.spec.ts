import { randomInt } from "node:crypto";
import { validCardNumber } from "../src/account-details";
import { expect } from "@playwright/test";
import { protectFixtureValues, test } from "./private-account-test";
import { api, expectNoOverflow, fictionalName, login, selectLedger } from "./helpers";
import type { Account } from "../src/ledger-api";

function fictionalCardNumber(suffix: string): string {
    const prefix = "0000" + randomInt(1_000_000, 10_000_000);
    for (let digit = 0; digit <= 9; digit++) {
        const number = `${prefix}${digit}${suffix}`;
        if (validCardNumber(number)) return number;
    }
    throw new Error("Unable to construct a fictional test card");
}

const cardFixtures = [
    { label: "Credit card", kind: "credit_card", suffix: "0018" },
    { label: "Debit card", kind: "debit_card", suffix: "0026" },
] as const;

test("card account drawers save, mask and time-limit a fictional full number", async ({
    page,
    context,
}) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.clock.install();
    await login(page);
    const entity = await api<{ id: string; name: string; ledger: { id: string } }>(
        page,
        "POST",
        "/api/v1/entities",
        { kind: "personal", name: fictionalName("card accounts"), base_asset_id: "USD" },
    );
    const accountsPath = `/api/v1/ledgers/${entity.ledger.id}/accounts`;
    await page.reload();
    await selectLedger(page, entity.name);
    await page.getByRole("button", { name: "Accounts", exact: true }).click();

    const reveals: string[] = [];
    page.on("request", (request) => {
        const path = new URL(request.url()).pathname;
        if (path.endsWith("/card-number")) reveals.push(path);
    });

    for (const fixture of cardFixtures) {
        const priorReveals = reveals.length;
        const number = fictionalCardNumber(fixture.suffix);
        protectFixtureValues(number);
        const name = fictionalName(fixture.label);
        const mask = `•••• ${fixture.suffix}`;
        await page.getByRole("button", { name: "New account", exact: true }).click();
        const create = page.getByRole("dialog", { name: "New account" });
        await create.getByLabel("Account name", { exact: true }).fill(name);
        await create.getByRole("radio", { name: fixture.label, exact: true }).check();
        await create.getByLabel("USD", { exact: true }).check();
        await create.getByLabel("Search issuers").fill("HSBCHKHH");
        await create.getByRole("radio", { name: /HSBC Hong Kong/i }).check();
        const logo = create.locator("[data-institution=hsbc-hk] .institution-logo");
        await expect(logo).toBeVisible();
        await expect
            .poll(() => logo.evaluate((image) => (image as HTMLImageElement).naturalWidth))
            .toBeGreaterThan(0);
        await create.getByLabel("Full card number").fill(number);
        await expect(create.getByLabel("Full card number")).toHaveAttribute("type", "password");
        await create.getByLabel("Expiration date").fill("01/37");
        await create.getByLabel("Cardholder name").fill("Fictional Cardholder");

        if (fixture.kind === "credit_card") {
            await create.getByLabel("Statement day").fill("20");
            await create.getByLabel("Payment due day").fill("5");
            await create.getByLabel("Credit limit").fill("1000.125");
        } else {
            await expect(
                create.getByText("A debit card has its own balance.", { exact: false }),
            ).toBeVisible();
            await create.getByLabel("Linked account number").fill("TEST-ACCOUNT");
        }
        await expectNoOverflow(page);

        const pendingSave = page.waitForResponse(
            (response) =>
                new URL(response.url()).pathname === accountsPath &&
                response.request().method() === "POST",
        );
        await create.getByRole("button", { name: "Save", exact: true }).click();
        const save = await pendingSave;
        expect(save.status()).toBe(201);
        expect((await save.text()).includes(number)).toBe(false);
        const account = (await save.json()) as Account;
        expect(account.kind).toBe(fixture.kind);
        expect(account.has_card_number).toBe(true);
        expect(account.details).toMatchObject({
            institution: "hsbc-hk",
            card_last_four: fixture.suffix,
            expiry: "01/37",
            cardholder: "Fictional Cardholder",
            ...(fixture.kind === "credit_card"
                ? { credit_limit: "1000.125" }
                : { linked_account_number: "TEST-ACCOUNT" }),
        });

        const listing = await page.request.get(accountsPath);
        expect(listing.status()).toBe(200);
        expect((await listing.text()).includes(number)).toBe(false);
        const card = page.getByTestId(`account-${account.id}`);
        await expect(card).toContainText(mask);
        await expect(card.locator("[data-institution=hsbc-hk] .institution-logo")).toBeVisible();
        await card.getByRole("button", { name: "Edit", exact: true }).click();
        const editor = page.getByRole("dialog", { name: "Edit account" });
        await expect(editor.getByLabel("Full card number")).toHaveValue("");
        await expect(editor.getByRole("button", { name: "Show card number" })).toHaveCount(0);
        await editor.getByRole("button", { name: "Cancel", exact: true }).click();

        await card.getByRole("button", { name: "Card details", exact: true }).click();
        const drawer = page.getByRole("dialog", { name: "Card details" });
        const savedNumber = drawer.getByLabel("Saved card number", { exact: true });
        await expect(drawer.locator("[data-institution=hsbc-hk] .institution-logo")).toBeVisible();
        await expect.poll(async () => (await savedNumber.textContent()) === mask).toBe(true);
        expect(reveals).toHaveLength(priorReveals);

        const pendingReveal = page.waitForResponse(
            (response) =>
                new URL(response.url()).pathname === `${accountsPath}/${account.id}/card-number`,
        );
        await drawer.getByRole("button", { name: "Show card number" }).click();
        expect((await pendingReveal).headers()["cache-control"]).toContain("no-store");
        expect(reveals.slice(priorReveals)).toEqual([`${accountsPath}/${account.id}/card-number`]);
        await expect.poll(async () => (await savedNumber.textContent()) === number).toBe(true);
        await drawer.getByRole("button", { name: "Copy card number" }).click();
        await expect(drawer.getByRole("status")).toContainText("Copied");
        expect((await page.evaluate(() => navigator.clipboard.readText())) === number).toBe(true);
        expect(
            await page.evaluate(
                (number) =>
                    [localStorage, sessionStorage].some((storage) =>
                        JSON.stringify(Object.values(storage)).includes(number),
                    ),
                number,
            ),
        ).toBe(false);
        await page.clock.fastForward(30_001);
        await expect.poll(async () => (await savedNumber.textContent()) === mask).toBe(true);
        await drawer.getByRole("button", { name: "Close", exact: true }).click();
    }
    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await page.getByRole("button", { name: "New entry", exact: true }).click();
    const accountOptions = page.getByLabel("Account", { exact: true });
    for (const { suffix, label } of cardFixtures) {
        const option = accountOptions.locator("option").filter({ hasText: "•••• " + suffix });
        await expect(option).toHaveAttribute("aria-label", new RegExp(label));
        await expect(option).toHaveAttribute("aria-description", "•••• " + suffix);
    }
});
