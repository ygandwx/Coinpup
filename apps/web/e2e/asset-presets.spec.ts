import { randomUUID } from "node:crypto";
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
        ["USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7", "6"],
        ["USDC", "ethereum", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "6"],
        ["USDC", "solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "6"],
        ["USDT", "avalanche", "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7", "6"],
        ["FDUSD", "bsc", "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409", "18"],
        ["FDUSD", "arbitrum", "0x93C9932E4afa59201F0B5E63f7d816516F1669fE", "18"],
        ["FDUSD", "ton", "EQD0Evpk4timFOHmy4Sv3l_KEUXlM-dN1_KhroTCfB2wkO89", "6"],
        ["DAI", "optimism", "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "18"],
        ["USDT", "tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "6"],
    ] as const;
    const requests: string[] = [];
    page.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/assets")
            requests.push(request.url());
    });
    await expect(pick.getByRole("option")).toHaveCount(21);
    for (const [code, network, reference, scale] of cases) {
        await pick.selectOption(`token:${network}:${reference}`);
        await page.getByRole("button", { name: "Fill in details" }).click();
        await expect(page.getByLabel("Asset code", { exact: true })).toHaveValue(code);
        await expect(page.getByLabel("Network", { exact: true })).toHaveValue(network);
        await expect(page.getByLabel("Token reference", { exact: true })).toHaveValue(reference);
        await expect(page.getByLabel("Decimal places", { exact: true })).toHaveValue(scale);
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
    const catalog = await api<{ asset_id: string; scale: number; verified: boolean }[]>(
        page,
        "GET",
        "/api/v1/assets",
    );
    expect(catalog).toEqual(
        expect.arrayContaining([
            expect.objectContaining({
                asset_id: "token:tron:TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                scale: 6,
                verified: true,
            }),
        ]),
    );
});

test("create a native asset and an explicitly unverified custom token", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    await page.getByRole("button", { name: "Assets", exact: true }).click();
    const newAsset = page.getByRole("button", { name: "New asset", exact: true });
    const save = page.getByRole("button", { name: "Save", exact: true });
    const waitForCreate = () =>
        page.waitForResponse(
            (response) =>
                new URL(response.url()).pathname === "/api/v1/assets" &&
                response.request().method() === "POST",
        );
    await newAsset.click();
    await page.getByLabel("Asset kind", { exact: true }).selectOption("native");
    const code = page.getByLabel("Asset code", { exact: true });
    await expect(code.getByRole("option")).toHaveCount(15);
    await code.selectOption("AVAX");
    const scale = page.getByLabel("Decimal places", { exact: true });
    const network = page.getByLabel("Network", { exact: true });
    await expect(scale).toHaveValue("18");
    await expect(scale).not.toBeEditable();
    await expect(network).toHaveValue("avalanche");
    await expect(network).not.toBeEditable();
    const nativeCreation = waitForCreate();
    await save.click();
    const nativeStatus = (await nativeCreation).status();
    expect([201, 409]).toContain(nativeStatus);
    const nativeId = "native:avalanche:AVAX";
    if (nativeStatus === 409) {
        await expect(page.getByRole("alert")).toBeVisible();
        await page.getByRole("button", { name: "Cancel", exact: true }).click();
        await page.reload();
        await page.getByRole("button", { name: "Assets", exact: true }).click();
    }
    const native = page.getByTestId(`asset-${nativeId}`);
    await expect(native.getByText("Avalanche C-Chain", { exact: true })).toBeVisible();
    await expect(native.locator("img.coin-image")).toHaveAttribute("src", /\/avax\.svg$/);

    await newAsset.click();
    const suffix = randomUUID().slice(0, 8);
    const reference = `FictionalMint${suffix}`;
    await code.fill(`Q${suffix.toLowerCase()}`);
    await expect(code).toHaveValue(`Q${suffix.toUpperCase()}`);
    await network.fill("fictional-network");
    await page.getByLabel("Token reference", { exact: true }).fill(reference);
    await scale.fill("0");
    const customCreation = waitForCreate();
    await save.click();
    expect((await customCreation).status()).toBe(201);
    const custom = page.getByTestId(`asset-token:fictional-network:${reference}`);
    await expect(custom.getByText("Custom · Unverified", { exact: true })).toBeVisible();
    await expect(custom.getByText(reference, { exact: true })).toBeVisible();
    await expect(custom.locator('[data-network="fictional-network"]')).toBeVisible();
    await expect(custom.locator("img.coin-image")).toHaveAttribute("src", /\/generic\.svg$/);
    await expectNoOverflow(page);
});
