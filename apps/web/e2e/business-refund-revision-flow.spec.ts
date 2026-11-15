import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { openRefundVersions, refundRevisionFixture } from "./refund-revision-helpers";
import type { RefundCurrent, RefundRevisionReceipt } from "../src/refund-api";

for (const locale of ["en", "zh"] as const)
    test(`fictional ${locale} corrects and cancels a real refund with exact-body recovery`, async ({
        page,
    }, info) => {
        const en = locale === "en",
            f = await refundRevisionFixture(page);
        let versions = await openRefundVersions(page, f.entity.id, locale);
        const paymentPath = "**" + f.ledger + "/business-payments/" + f.original.payment_id;
        const unexpectedWrites: string[] = [];
        const watch = (req: import("@playwright/test").Request) => {
            if (req.method() === "POST" && req.url().includes(f.refundPath + "/"))
                unexpectedWrites.push(req.url());
        };
        page.on("request", watch);
        await page.route(
            paymentPath,
            async (route) => {
                const actual = await route.fetch(),
                    body = await actual.json();
                expect(actual.status()).toBe(200);
                await route.fulfill({
                    response: actual,
                    json: { ...body, receipt: { ...body.receipt, kind: "business_refund" } },
                });
            },
            { times: 1 },
        );
        await versions
            .getByRole("button", { name: en ? "Correct this refund" : "更正此笔退款", exact: true })
            .click();
        await expect(versions.getByRole("alert")).toBeVisible();
        await expect(
            versions.getByRole("form", { name: en ? "Correct refund facts" : "更正退款事实" }),
        ).toHaveCount(0);
        expect(unexpectedWrites).toEqual([]);
        page.off("request", watch);
        await versions
            .getByRole("button", { name: en ? "Reload sources" : "重新读取来源", exact: true })
            .click();
        const form = versions.getByRole("form", {
            name: en ? "Correct refund facts" : "更正退款事实",
            exact: true,
        });
        await expect(
            form.getByText(
                en ? "Replacement limit including this refund" : "含本笔现有本金的替代上限",
                { exact: true },
            ),
        ).toBeVisible();
        const confirm = form.getByRole("button", {
            name: en ? "Confirm refund correction" : "确认更正退款",
            exact: true,
        });
        await expect(confirm).toBeDisabled();
        await form
            .getByLabel(en ? "Correction reason" : "更正原因", { exact: true })
            .fill("Fictional correction 中文");
        await form
            .getByLabel(en ? "Actual refund quantity" : "实际退款数量", { exact: true })
            .fill("95.01");
        await form
            .getByLabel(en ? "Original principal being refunded (USD)" : "退还原付款本金 (USD)", {
                exact: true,
            })
            .fill("95.01");
        await form
            .getByLabel(en ? "Refund reason" : "退款原因", { exact: true })
            .fill("Fictional replacement cash 中文");
        await form
            .getByRole("checkbox", { name: en ? /I reviewed original principal/ : /已核对原本金/ })
            .check();
        const path = f.refundPath + "/corrections",
            sent: { key: string; body: string }[] = [];
        await page.route("**" + path, async (route) => {
            const request = route.request();
            sent.push({ key: request.headers()["idempotency-key"], body: request.postData()! });
            if (sent.length === 1) {
                const actual = await route.fetch();
                expect(actual.status()).toBe(200);
                await route.abort("failed");
            } else await route.continue();
        });
        await confirm.click();
        const retry = page.getByRole("button", {
            name: en ? "Retry original request" : "重试原请求",
            exact: true,
        });
        await expect(retry).toBeVisible();
        const afterLost = await api<RefundCurrent>(page, "GET", f.refundPath + "/current");
        expect(afterLost.version).toBe(2);
        expect(afterLost.fact?.payment_amount).toBe("95.01");
        for (const width of info.project.name === "mobile-chromium" ? [375, 320] : [1440]) {
            await page.setViewportSize({ width, height: 900 });
            await expectNoOverflow(page);
            const name = `refund-unknown-${locale}-${width}`,
                file = info.outputPath(name + ".png");
            await retry.locator("xpath=ancestor::section[1]").screenshot({ path: file });
            await info.attach(name, { path: file, contentType: "image/png" });
        }
        await retry.click();
        await expect(
            page.getByRole("status").filter({
                hasText: en ? /Verified saved receipt, version 2/ : /已核对固定回执，版本 2/,
            }),
        ).toBeVisible();
        expect(sent).toHaveLength(2);
        expect(sent[0]).toEqual(sent[1]);
        expect(sent[0].key).toBeTruthy();
        expect(JSON.parse(sent[0].body)).toMatchObject({
            expected_version: 1,
            reason: "Fictional correction 中文",
            replacement: {
                amount: "95.01",
                payment_amount: "95.01",
                reason: "Fictional replacement cash 中文",
            },
        });
        await page
            .getByRole("button", { name: en ? "Back to settlements" : "返回收付列表", exact: true })
            .click();
        versions = await openRefundVersions(page, f.entity.id, locale);
        await versions
            .getByRole("button", { name: en ? "Cancel this refund" : "取消此笔退款", exact: true })
            .click();
        const cancel = versions.getByRole("button", {
            name: en ? "Confirm refund cancellation" : "确认取消退款",
            exact: true,
        });
        await expect(cancel).toBeDisabled();
        await versions
            .getByLabel(en ? "Cancellation reason" : "取消原因", { exact: true })
            .fill("Fictional final cancel 中文");
        await versions
            .getByRole("checkbox", { name: en ? /I reviewed this refund/ : /我已核对当前退款/ })
            .check();
        const response = page.waitForResponse(
            (r) =>
                r.url().endsWith(f.refundPath + "/cancellations") &&
                r.request().method() === "POST",
        );
        await cancel.click();
        const http = await response;
        expect(http.status()).toBe(200);
        const saved = (await http.json()) as RefundRevisionReceipt;
        expect(saved.action).toBe("cancel");
        expect(saved.previous_journal_id).toBe(afterLost.journal_id);
        expect(saved.replacement).toBeNull();
        await expect(
            page.getByRole("status").filter({
                hasText: en ? /Verified saved receipt, version 3/ : /已核对固定回执，版本 3/,
            }),
        ).toBeVisible();
        const end = await api<RefundCurrent>(page, "GET", f.refundPath + "/current");
        expect(end.status).toBe("cancelled");
        expect(end.fact).toBeNull();
    });
