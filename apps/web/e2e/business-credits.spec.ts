import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessConfirmationReceipt } from "../src/business-confirmation-api";
import type { CreditSummary, CreditDetail, CreditCapacity } from "../src/credit-api";
import type { Balance } from "../src/ledger-api";

for (const [kind, principal] of [
    ["invoice", "100"],
    ["bill", "100"],
    ["invoice", "90000000000000000000"],
] as const)
    test(`fictional ${kind}/${principal} manual credit and exact retry`, async ({ page }, info) => {
        test.setTimeout(120_000);
        const data = await confirmationSetup(page, kind, principal);
        const confirmed = await api<BusinessConfirmationReceipt>(
            page,
            "POST",
            `${data.base}/confirm`,
            {
                id: randomUUID(),
                expected_version: 1,
            },
        );
        const original = await api(page, "GET", `${data.base}/confirmation`);
        await api(page, "POST", `${data.ledger}/accounts`, {
            name: "Fictional credit cash remains unchanged",
            kind: "bank",
            asset_ids: ["USD"],
        });
        const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
        expect(balances).toHaveLength(1);
        await page.goto(`/?entity=${data.entity.id}&view=business-credits`);
        let readFailed = false;
        await page.route(`**${data.base}/credit-capacity?*`, (route) => {
            if (!readFailed) {
                readFailed = true;
                return route.abort("failed");
            }
            return route.continue();
        });
        await page.getByRole("button", { name: "Credit this document", exact: true }).click();
        await expect(page.getByRole("alert")).toBeVisible();
        await page.getByRole("button", { name: "Reload", exact: true }).click();
        const form = page.getByRole("form", { name: "Record credit", exact: true });
        await expect(form).toBeVisible();
        await page.getByLabel("Credit transaction date", { exact: true }).fill("2026-01-05");
        await page
            .getByLabel("Credit reason", { exact: true })
            .fill("Fictional manual credit 中文 " + "reason ".repeat(8));
        await page.getByRole("checkbox", { name: "Credit this line", exact: true }).check();
        await page.getByLabel("Credit recognition date", { exact: true }).fill("2026-01-05");
        const reviewed = page.getByRole("checkbox", {
            name: "I reviewed the original classification",
            exact: false,
        });
        let writes = 0;
        const count = (request: { method(): string; url(): string }) => {
            if (request.method() === "POST" && request.url().endsWith("/business-credits"))
                writes += 1;
        };
        page.on("request", count);
        for (const [net, tax] of [
            ["0.001", "0"],
            ["1", principal === "100" ? "10.01" : "9000000000000000000.01"],
        ]) {
            await page.getByLabel("Credit net amount", { exact: true }).fill(net);
            await page.getByLabel("Credit tax amount", { exact: true }).fill(tax);
            await reviewed.check();
            await page.getByRole("button", { name: "Record credit", exact: true }).click();
            await expect(form.getByRole("alert")).toBeVisible();
            expect(writes).toBe(0);
        }
        page.off("request", count);
        await page.getByLabel("Credit net amount", { exact: true }).fill("17.23");
        await page.getByLabel("Credit tax amount", { exact: true }).fill("2.77");
        await reviewed.check();
        for (const width of [1440, 375, 320])
            for (const locale of ["zh", "en"]) {
                await page.setViewportSize({ width, height: 1000 });
                await page
                    .getByRole("button", {
                        name: locale === "zh" ? "中文" : "English",
                        exact: true,
                    })
                    .click();
                await expect(
                    page.getByLabel(locale === "zh" ? "本次贷项净额" : "Credit net amount", {
                        exact: true,
                    }),
                ).toHaveValue("17.23");
                await expect(
                    page.getByLabel(locale === "zh" ? "本次贷项税额" : "Credit tax amount", {
                        exact: true,
                    }),
                ).toHaveValue("2.77");
                const reason = page.getByLabel(locale === "zh" ? "贷项原因" : "Credit reason", {
                    exact: true,
                });
                await expect(reason).toHaveValue(/Fictional manual credit 中文/u);
                await expect(reason).toHaveAccessibleName(
                    locale === "zh" ? "贷项原因" : "Credit reason",
                );
                const review = page.getByRole("checkbox", {
                    name:
                        locale === "zh"
                            ? "我已核对原分类"
                            : "I reviewed the original classification",
                    exact: false,
                });
                await expect(review).toBeChecked();
                if (width < 600) {
                    for (const actions of await page
                        .locator("form.credit-form .form-actions")
                        .all())
                        await expect(actions).toHaveCSS("display", "grid");
                    for (const name of locale === "zh"
                        ? ["登记贷项", "取消"]
                        : ["Record credit", "Cancel"]) {
                        const bounds = await page
                            .getByRole("button", { name, exact: true })
                            .boundingBox();
                        expect(bounds!.width).toBeGreaterThan(120);
                        expect(bounds!.height).toBeLessThanOrEqual(80);
                    }
                }
                await expectNoOverflow(page);
                await page.screenshot({
                    path: info.outputPath(`credit-${kind}-${locale}-${width}.png`),
                    fullPage: true,
                });
            }
        const bodies: string[] = [],
            keys: string[] = [];
        await page.route(`**${data.ledger}/business-credits`, async (route) => {
            if (route.request().method() !== "POST") return route.continue();
            bodies.push(route.request().postData()!);
            keys.push(route.request().headers()["idempotency-key"]);
            if (bodies.length === 1) {
                const response = await route.fetch();
                expect(response.status()).toBe(201);
                return route.abort("failed");
            }
            if (bodies.length === 2) {
                const session = await (await page.request.get("/api/v1/auth/session")).json();
                expect(
                    (
                        await page.request.post("/api/v1/auth/logout", {
                            headers: {
                                Origin: new URL(page.url()).origin,
                                "X-CSRF-Token": session.csrf_token,
                            },
                        })
                    ).status(),
                ).toBe(204);
            }
            await route.continue();
        });
        await page
            .getByRole("button", { name: "Record credit", exact: true })
            .evaluate((button) => {
                (button as HTMLButtonElement).click();
                (button as HTMLButtonElement).click();
            });
        await expect(
            page.getByRole("button", { name: "Retry original request", exact: true }),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Transactions", exact: true }),
        ).toBeDisabled();
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`credit-unknown-${kind}-en-320.png`),
            fullPage: true,
        });
        await page.getByRole("button", { name: "Retry original request", exact: true }).click();
        await page.getByLabel("Username", { exact: true }).fill(username);
        await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
        await page.getByRole("button", { name: "Sign in", exact: true }).click();
        await page.getByRole("button", { name: "Retry original request", exact: true }).click();
        await expect(
            page.getByText(
                "The original credit request is saved; its complete line receipt is verified.",
                { exact: true },
            ),
        ).toBeVisible();
        expect(bodies).toHaveLength(3);
        expect(new Set(bodies).size).toBe(1);
        expect(new Set(keys).size).toBe(1);
        const credits = await api<CreditSummary[]>(page, "GET", `${data.ledger}/business-credits`);
        expect(credits).toHaveLength(1);
        const saved = await api<CreditDetail>(
            page,
            "GET",
            `${data.ledger}/business-credits/${credits[0].id}`,
        );
        expect(saved.receipt.postings[0]).toMatchObject({
            source_line_id: data.source.lines[0].id,
            source_operation_id: confirmed.postings[0].operation_id,
            net_amount: "17.230000000000000000",
            tax_amount: "2.770000000000000000",
            total_amount: "20.000000000000000000",
        });
        expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(original);
        expect(await api(page, "GET", `${data.ledger}/balances?limit=100`)).toEqual(balances);
        const capacity = await api<CreditCapacity>(page, "GET", `${data.base}/credit-capacity`);
        expect(capacity.lines[0].credited_net_amount).toBe("17.23");
        expect(capacity.lines[0].credited_tax_amount).toBe("2.77");
        let historyFailed = false;
        await page.route(`**${data.ledger}/business-credits?*`, (route) => {
            if (!historyFailed) {
                historyFailed = true;
                return route.abort("failed");
            }
            return route.continue();
        });
        await page.getByRole("button", { name: "Back to credit list", exact: true }).click();
        await expect(page.getByRole("alert")).toBeVisible();
        await page.getByRole("button", { name: "Reload", exact: true }).click();
        await page.getByRole("button", { name: "View credit details", exact: true }).click();
        await expect(page.getByRole("region", { name: "Credits", exact: true })).toContainText(
            "17.23",
        );
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`credit-history-${kind}-en-320.png`),
            fullPage: true,
        });
        await page.getByRole("button", { name: "Back to credit list", exact: true }).click();
        await page.getByRole("button", { name: "Choose original document", exact: true }).click();
        await page.getByRole("button", { name: "Credit this document", exact: true }).click();
        await page.getByLabel("Credit transaction date", { exact: true }).fill("2026-01-05");
        await page.getByLabel("Credit reason", { exact: true }).fill("Fictional closed credit");
        await page.getByRole("checkbox", { name: "Credit this line", exact: true }).check();
        await page.getByLabel("Credit net amount", { exact: true }).fill("1");
        await page.getByLabel("Credit tax amount", { exact: true }).fill("0");
        await page.getByLabel("Credit recognition date", { exact: true }).fill("2026-01-05");
        await api(page, "POST", `${data.ledger}/period-changes`, {
            action: "close",
            expected_version: 1,
            closed_through: "2026-01-05",
            reason: "Fictional credit period rejection",
        });
        await page.unroute(`**${data.ledger}/business-credits`);
        await reviewed.check();
        await page.getByRole("button", { name: "Record credit", exact: true }).click();
        await expect(page.getByRole("alert")).toContainText("closed");
        for (const [locale, width] of [
            ["zh", 320],
            ["en", 375],
        ] as const) {
            await page.setViewportSize({ width, height: 1000 });
            await page
                .getByRole("button", { name: locale === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`credit-period-${kind}-${locale}-${width}.png`),
                fullPage: true,
            });
        }
        await page.getByRole("button", { name: "Edit rejected request", exact: true }).click();
        await expect(page.getByLabel("Credit net amount", { exact: true })).toHaveValue("1");
        await expect(page.getByLabel("Credit tax amount", { exact: true })).toHaveValue("0");
        await expect(page.getByLabel("Credit transaction date", { exact: true })).toHaveValue(
            "2026-01-05",
        );
        expect(await api(page, "GET", `${data.ledger}/business-credits`)).toEqual(credits);
    });
