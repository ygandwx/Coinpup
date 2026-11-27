import { expect, test } from "@playwright/test";

import { api, expectNoOverflow, login } from "./helpers";

test("stablecoin presets fill the form and keep ordinary asset creation", async ({ page }) => {
    await page.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
    await login(page);
    await page.getByRole("button", { name: "Assets", exact: true }).click();
    await page.getByRole("button", { name: "Add from preset", exact: true }).click();
    const pick = page.getByLabel("Stablecoin preset", { exact: true });
    const cases = [
        ["usdt-ethereum", "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7"],
        ["usdc-ethereum", "USDC", "ethereum", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"],
        ["usdc-solana", "USDC", "solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"],
        ["usdt-tron", "USDT", "tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"],
    ];
    const requests: string[] = [];
    page.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/assets")
            requests.push(request.url());
    });
    for (const [id, code, network, reference] of cases) {
        await pick.selectOption(id);
        await page.getByRole("button", { name: "Fill in details" }).click();
        await expect(page.getByLabel("Asset code", { exact: true })).toHaveValue(code);
        await expect(page.getByLabel("Network", { exact: true })).toHaveValue(network);
        await expect(page.getByLabel("Token reference", { exact: true })).toHaveValue(reference);
        await expect(page.getByLabel("Decimal places", { exact: true })).toHaveValue("6");
    }
    expect(requests).toHaveLength(0);
    await expectNoOverflow(page);
    const result = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === "/api/v1/assets" &&
            response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Save", exact: true }).click();
    // The desktop and mobile projects share the disposable asset catalog.
    expect([201, 409]).toContain((await result).status());
    expect(requests).toHaveLength(1);
    const catalog = await api<{ asset_id: string; scale: number }[]>(page, "GET", "/api/v1/assets");
    expect(catalog).toEqual(
        expect.arrayContaining([
            expect.objectContaining({
                asset_id: "token:tron:TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                scale: 6,
            }),
        ]),
    );
});
