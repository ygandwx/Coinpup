import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { openRefundVersions, refundRevisionFixture } from "./refund-revision-helpers";
import type { RefundRevisionReceipt } from "../src/refund-api";

for (const locale of ["en", "zh"] as const)
    test(`fictional ${locale} refund shows current cancellation and the exact prior correction`, async ({
        page,
    }, info) => {
        const f = await refundRevisionFixture(page);
        const saved = await api<RefundRevisionReceipt>(
            page,
            "POST",
            f.refundPath + "/corrections",
            {
                expected_version: 1,
                reason: "Fictional earlier correction 中文",
                replacement: {
                    account_id: f.bank.id,
                    asset_id: "USD",
                    amount: "10.01",
                    payment_amount: "10.01",
                    transaction_date: "2026-01-04",
                    reason: "Fictional replacement cash 中文",
                    fees: [],
                    allocation_return_ids: [],
                },
            },
        );
        const en = locale === "en";
        let commands = 0;
        page.on("request", (r) => {
            if (r.method() === "POST" && r.url().includes(f.refundPath)) commands++;
        });
        const activePanel = await openRefundVersions(page, f.entity.id, locale);
        const activeVersion = activePanel.getByRole("heading", {
            name: en ? "Current version 2 · Active" : "当前版本 2 · 有效",
        });
        const correct = activePanel.getByRole("button", {
            name: en ? "Correct this refund" : "更正此笔退款",
            exact: true,
        });
        const cancel = activePanel.getByRole("button", {
            name: en ? "Cancel this refund" : "取消此笔退款",
            exact: true,
        });
        await expect(activeVersion).toBeVisible();
        await expect(correct).toBeVisible();
        await expect(cancel).toBeVisible();
        await page.route(
            "**" + f.refundPath + "/current",
            async (route) => {
                const response = await route.fetch();
                expect(response.status()).toBe(200);
                const body = await response.json();
                expect(body.refund_id).toBe(f.original.id);
                expect(body.payment_id).not.toBe(f.original.id);
                await route.fulfill({ response, json: { ...body, payment_id: f.original.id } });
            },
            { times: 1 },
        );
        await activePanel
            .getByRole("button", { name: en ? "Current refund" : "当前退款", exact: true })
            .click();
        await expect(activePanel.getByRole("alert")).toBeVisible();
        await expect(correct).toHaveCount(0);
        await expect(cancel).toHaveCount(0);
        await expect(activeVersion).toHaveCount(0);
        await activePanel
            .getByRole("button", { name: en ? "Reload version" : "重新读取版本", exact: true })
            .click();
        await expect(activeVersion).toBeVisible();
        await expect(correct).toBeVisible();
        await expect(cancel).toBeVisible();
        expect(commands).toBe(0);
        await api(page, "POST", f.refundPath + "/cancellations", {
            expected_version: 2,
            reason: "Fictional final cancel 中文",
        });
        const width = info.project.name === "mobile-chromium" ? 320 : 1440;
        await page.setViewportSize({ width, height: 900 });
        const panel = await openRefundVersions(page, f.entity.id, locale);
        await expect(
            panel.getByRole("heading", {
                name: en ? "Current version 3 · Cancelled" : "当前版本 3 · 已取消",
            }),
        ).toBeVisible();
        await expect(panel.getByText("Fictional final cancel 中文", { exact: true })).toBeVisible();
        await panel
            .getByRole("button", { name: en ? "Version history" : "版本历史", exact: true })
            .click();
        await expect(panel.getByRole("listitem")).toHaveCount(3);
        await panel
            .getByRole("button", {
                name: en ? "View revision receipt" : "查看修订回执",
                exact: true,
            })
            .first()
            .click();
        await expect(
            panel.getByRole("heading", {
                name: en ? "Saved revision receipt · 1 → 2" : "固定修订回执 · 1 → 2",
            }),
        ).toBeVisible();
        await expect(panel.getByText(saved.reversal_journal_id, { exact: false })).toBeVisible();
        await expect(panel.getByText(saved.previous_journal_id, { exact: false })).toBeVisible();
        await expect(
            panel.getByText(saved.replacement_journal_id!, { exact: false }),
        ).toBeVisible();
        await expect(
            panel.getByText("Fictional replacement cash 中文", { exact: false }),
        ).toBeVisible();
        await expect(panel.getByText(/10\.01 USD/).first()).toBeVisible();
        await expect(panel.getByText(/10\.010000/)).toHaveCount(0);
        for (const layout of info.project.name === "mobile-chromium" ? [375, 320] : [1440]) {
            await page.setViewportSize({ width: layout, height: 900 });
            await expectNoOverflow(page);
            const name = "refund-version-" + locale + "-" + layout;
            const path = info.outputPath(name + ".png");
            await panel.screenshot({ path });
            await info.attach(name, { path, contentType: "image/png" });
        }
        await panel
            .getByRole("button", {
                name: en ? "Back to version page" : "返回此页版本",
                exact: true,
            })
            .click();
        await expect(panel.getByRole("listitem")).toHaveCount(3);
        await expect(
            panel.getByRole("button", { name: en ? "Next versions" : "下一页版本", exact: true }),
        ).toBeDisabled();
        expect(commands).toBe(0);
    });
