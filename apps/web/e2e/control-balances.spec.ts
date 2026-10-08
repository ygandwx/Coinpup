import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, login } from "./helpers";
import type { ControlBalance } from "../src/ledger-api";

test("fictional controls remain separate by direction and document in both languages and screen sizes", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = JSON.parse(
        execFileSync(
            "docker",
            [
                "compose",
                "exec",
                "-T",
                "-e",
                "COINPUP_ENVIRONMENT=test",
                "-e",
                "COINPUP_CREATE_CONTROL_BROWSER_FIXTURE=1",
                "api",
                "python",
                "scripts/create_control_browser_fixture.py",
            ],
            { encoding: "utf8" },
        ),
    );
    const base = `/api/v1/ledgers/${fixture.ledger}`;
    const balances = await api<ControlBalance[]>(page, "GET", `${base}/control-balances`);
    expect(balances).toHaveLength(8);
    await page.goto(`/?entity=${fixture.entity}&view=controls`);
    await expect(page.getByTestId("control-balance")).toHaveCount(8);
    await page.getByLabel("Control direction", { exact: true }).selectOption("payable.supplier");
    const cards = page.getByTestId("control-balance");
    await expect(cards).toHaveCount(2);
    expect(await cards.getByTestId("control-amount").allTextContents()).toEqual(
        expect.arrayContaining(["100.00 USD", "40.00 USD"]),
    );
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const locale of ["zh", "en"]) {
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expect(
                page.getByLabel(locale === "zh" ? "往来方向" : "Control direction", {
                    exact: true,
                }),
            ).toHaveValue("payable.supplier");
            await expect(cards).toHaveCount(2);
            await expect(cards.filter({ hasText: "-100.00 USD" })).toHaveCount(1);
            await expect(cards.filter({ hasText: "-40.00 USD" })).toHaveCount(1);
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`control-balances-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    }
    await page.getByLabel("Control direction", { exact: true }).selectOption("");
    await expect(cards).toHaveCount(8);
    expect(await api(page, "GET", `${base}/accounts`)).toEqual([]);
    expect(await api(page, "GET", `${base}/balances`)).toEqual([]);
    expect(await api(page, "GET", `${base}/control-balances`)).toEqual(balances);
    await page.goto(`/?entity=${fixture.entity}&view=accounts`);
    await expect(
        page.getByRole("heading", { name: "Payment accounts", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Supplier payables", { exact: true })).toHaveCount(0);
});
