import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { Project } from "../src/business-api";
import type { Account } from "../src/ledger-api";
import type { BusinessDraft } from "../src/business-draft-api";

test("archived project shows recognized costs and partial payments in their own periods and assets", async ({
    page,
}, info) => {
    const s = await confirmationSetup(page, "bill");
    const project = await api<Project>(page, "POST", s.ledger + "/business-projects", {
        id: randomUUID(),
        name: "Fictional 设计项目",
    });
    const account = await api<Account>(page, "POST", s.ledger + "/accounts", {
        name: "Fictional project bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    await api(page, "POST", s.ledger + "/opening-balances", {
        account_id: account.id,
        asset_id: "USD",
        amount: "1000",
        transaction_date: "2026-01-01",
        description: "Fictional opening",
    });
    for (const [asset, price] of [
        ["USD", "300"],
        ["BTC", "0.00000001"],
    ]) {
        const doc = await api<BusinessDraft>(page, "POST", s.ledger + "/business-documents", {
            id: randomUUID(),
            document_kind: "bill",
            party_id: s.party.id,
            asset_id: asset,
            issue_date: "2026-09-30",
            lines: [
                {
                    id: randomUUID(),
                    description: "Fictional cost",
                    quantity: "1",
                    unit_price: price,
                    tax_rate_percent: "0",
                    project_id: project.id,
                    category_id: s.source.lines[0].category_id,
                    recognition_date: "2026-09-30",
                },
            ],
        });
        await api(page, "POST", s.ledger + "/business-documents/" + doc.id + "/confirm", {
            id: randomUUID(),
            expected_version: doc.version,
        });
        if (asset === "USD")
            for (const [day, amount] of [
                ["2026-09-30", "100"],
                ["2026-10-01", "50"],
            ]) {
                await api(page, "POST", s.ledger + "/business-payments", {
                    id: randomUUID(),
                    direction: "payout",
                    party_id: s.party.id,
                    account_id: account.id,
                    asset_id: "USD",
                    amount,
                    transaction_date: day,
                    description: "Fictional partial payment",
                    initial_allocation: {
                        id: randomUUID(),
                        allocation_date: day,
                        lines: [
                            {
                                document_id: doc.id,
                                document_line_id: doc.lines[0].id,
                                document_amount: amount,
                                payment_amount: amount,
                            },
                        ],
                    },
                });
            }
    }
    await api(page, "PATCH", s.ledger + "/business-projects/" + project.id, {
        expected_version: project.version,
        archived: true,
    });
    const other = await api<Project>(page, "POST", s.ledger + "/business-projects", {
        id: randomUUID(),
        name: "Fictional empty project",
    });
    await page.goto("/?entity=" + s.entity.id + "&view=masters");
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    await page.getByLabel("Record type", { exact: true }).selectOption("projects");
    const open = async (name: string) =>
        page
            .getByRole("listitem")
            .filter({ hasText: name })
            .getByRole("button", { name: "View project activity", exact: true })
            .click();
    await open(project.name!);
    const start = page.getByLabel("Start date", { exact: true }),
        end = page.getByLabel("End date", { exact: true });
    const usd = page
        .locator(".project-report li")
        .filter({ has: page.getByRole("heading", { name: "USD", exact: true }) });
    await start.fill("2026-09-01");
    await end.fill("2026-09-30");
    await button("Show activity").press("Enter");
    await expect(usd.locator("dd")).toHaveText([
        "0.00",
        "300.00",
        "-300.00",
        "0.00",
        "200.00",
        "0.00",
        "100.00",
    ]);
    const btc = page
        .locator(".project-report li")
        .filter({ has: page.getByRole("heading", { name: "BTC", exact: true }) });
    await expect(btc.locator("dd")).toHaveText([
        "0.00000000",
        "0.00000001",
        "-0.00000001",
        "0.00000000",
        "0.00000001",
        "0.00000000",
        "0.00000000",
    ]);
    for (const width of [1440, 375, 320])
        for (const lang of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 950 });
            await button(lang === "zh" ? "中文" : "English").click();
            await expect(
                usd.getByText(lang === "zh" ? "期末未付" : "Closing payables", { exact: true }),
            ).toBeVisible();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath("project-payment-" + lang + "-" + width + ".png"),
                fullPage: true,
            });
        }
    await start.fill("2026-10-01");
    await expect(button("Show activity")).toBeDisabled();
    await expect(usd).toHaveCount(0);
    await end.fill("2026-10-31");
    await button("Show activity").click();
    await expect(usd.locator("dd")).toHaveText([
        "0.00",
        "0.00",
        "0.00",
        "0.00",
        "150.00",
        "0.00",
        "50.00",
    ]);
    const path = "**" + s.ledger + "/business-projects/" + project.id + "/activity?*";
    await page.route(path, async (r) => {
        const response = await r.fetch();
        await r.fulfill({ response, json: { ...(await response.json()), project_id: other.id } });
    });
    await button("Show activity").click();
    await expect(page.getByRole("alert")).toContainText("Could not read this period");
    await expect(usd).toHaveCount(0);
    await page.unroute(path);
    await button("Back to projects").click();
    await open(other.name!);
    await start.fill("2026-09-01");
    await end.fill("2026-10-31");
    await button("Show activity").click();
    await expect(
        page.getByText("No activity or closing balances for this period.", { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".project-report li")).toHaveCount(0);
});
