import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { openReturnVersions, returnRevisionFixture } from "./return-revision-helpers";
import type { components } from "../src/generated/openapi";
type Wire = components["schemas"];

async function capture(page: Page, panel: Locator, info: TestInfo, label: string) {
    for (const width of info.project.name === "mobile-chromium" ? [375, 320] : [1440]) {
        await page.setViewportSize({ width, height: width === 1440 ? 1000 : 800 });
        await expectNoOverflow(page);
        const path = info.outputPath(label + "-" + width + ".png");
        await panel.screenshot({ path });
        await info.attach(label + "-" + width, { path, contentType: "image/png" });
    }
}
for (const locale of ["en", "zh"] as const)
    test(
        "fictional " +
            locale +
            " replaces two-asset return sources and recovers the exact commit before cancelling",
        async ({ page }, info) => {
            const en = locale === "en",
                f = await returnRevisionFixture(page);
            let panel = await openReturnVersions(page, f.entity.id, locale);
            await panel
                .getByRole("button", {
                    name: en ? "Correct this return group" : "更正此组退回",
                    exact: true,
                })
                .click();
            const form = panel.getByRole("form", { name: en ? "Return correction" : "退回更正" });
            await form
                .getByLabel(en ? "Revision reason" : "修订原因", { exact: true })
                .fill(" Fictional reviewed change 中文 ");
            await form
                .getByLabel(en ? "Replacement return reason" : "替代退回原因")
                .fill(" Fictional source swap 中文 ");
            await form
                .getByLabel(en ? "Replacement return date" : "替代退回日期")
                .fill("2026-01-05");
            const sources = form.getByRole("combobox");
            await expect(sources.locator("option")).toHaveCount(2);
            await sources.selectOption(f.lines[1].source_allocation_line_id);
            await form
                .getByRole("button", { name: en ? "Add source" : "添加来源", exact: true })
                .click();
            const rows = form.getByRole("listitem");
            await expect(rows).toHaveCount(2);
            await rows
                .nth(1)
                .getByLabel(en ? "Payment quantity · USD" : "付款侧数量 · USD")
                .fill("11.00");
            await rows
                .nth(1)
                .getByLabel(en ? "Document quantity · EUR" : "单据侧数量 · EUR")
                .fill("10.00");
            await rows
                .nth(1)
                .getByRole("button", { name: en ? "Move up" : "上移", exact: true })
                .click();
            await expect(rows.nth(0)).toContainText(f.lines[1].source_allocation_line_id);
            await rows
                .nth(1)
                .getByRole("button", { name: en ? "Remove source" : "移除来源", exact: true })
                .click();
            const check = form.getByRole("checkbox");
            await check.check();
            await capture(page, form, info, "return-edit-" + locale);
            const originalBody = {
                expected_version: 1,
                reason: " Fictional reviewed change 中文 ",
                replacement: {
                    return_date: "2026-01-05",
                    reason: " Fictional source swap 中文 ",
                    lines: [f.lines[1]],
                },
            };
            const requests: { body: string; key: string | null }[] = [];
            let saved: Wire["AllocationReturnRevisionResponse"] | undefined;
            const url = "**" + f.path + "/corrections";
            await page.route(url, async (route) => {
                const req = route.request();
                requests.push({
                    body: req.postData()!,
                    key: await req.headerValue("Idempotency-Key"),
                });
                const actual = await route.fetch();
                expect(actual.status()).toBe(200);
                const payload = await actual.json();
                if (requests.length === 1) {
                    saved = payload;
                    await route.abort("failed");
                } else {
                    expect(payload).toEqual(saved);
                    await route.fulfill({ response: actual });
                }
            });
            await form
                .getByRole("button", {
                    name: en ? "Submit return correction" : "提交退回更正",
                    exact: true,
                })
                .click();
            const retry = page.getByRole("button", {
                name: en ? "Retry original request" : "重试原请求",
                exact: true,
            });
            await expect(retry).toBeVisible();
            await retry.click();
            const result = page.locator(".settlement-result");
            await expect(result.getByRole("status")).toContainText(
                en ? "Verified saved receipt, version" : "已核对固定回执，版本",
            );
            expect(requests).toHaveLength(2);
            expect(requests[0].key).toBeTruthy();
            expect(requests[0]).toEqual(requests[1]);
            expect(JSON.parse(requests[0].body)).toEqual(originalBody);
            expect(saved!.members.map((m) => m.action)).toEqual(["create", "cancel"]);
            await expect(result).toContainText(saved!.revision_id);
            await expect(result.getByRole("listitem").first()).toContainText(
                "11.00 USD / 10.00 EUR",
            );
            await capture(page, result, info, "return-saved-" + locale);
            await page.unroute(url);
            const current = await api<Wire["AllocationReturnCurrent"]>(
                page,
                "GET",
                f.path + "/current",
            );
            expect(current.version).toBe(2);
            expect(current.members.map((m) => m.source_allocation_line_id)).toEqual([
                f.lines[1].source_allocation_line_id,
            ]);
            const original = await api<Wire["AllocationReturnDetail"]>(page, "GET", f.path);
            expect(original.receipt).toEqual(f.original);
            await result
                .getByRole("button", {
                    name: en ? "Back to settlements" : "返回收付列表",
                    exact: true,
                })
                .click();
            panel = await openReturnVersions(page, f.entity.id, locale);
            await panel
                .getByRole("button", {
                    name: en ? "Cancel this return group" : "取消此组退回",
                    exact: true,
                })
                .click();
            const cancel = panel.getByRole("form", {
                name: en ? "Return cancellation" : "退回取消",
            });
            await cancel
                .getByLabel(en ? "Revision reason" : "修订原因", { exact: true })
                .fill("Fictional terminal return 中文");
            await cancel.getByRole("checkbox").check();
            await cancel
                .getByRole("button", {
                    name: en ? "Submit return cancellation" : "提交退回取消",
                    exact: true,
                })
                .click();
            await expect(result.getByRole("status")).toContainText("3");
            const terminal = await api<Wire["AllocationReturnCurrent"]>(
                page,
                "GET",
                f.path + "/current",
            );
            expect(terminal).toMatchObject({
                status: "cancelled",
                version: 3,
                fact: null,
                members: [],
            });
            const history = await api<Wire["AllocationReturnHistoryEntry"][]>(
                page,
                "GET",
                f.path + "/history",
            );
            expect(history.map((x) => x.action)).toEqual(["create", "correct", "cancel"]);
            expect(await api(page, "GET", f.path + "/revisions/" + saved!.revision_id)).toEqual(
                saved,
            );
            await capture(page, result, info, "return-cancelled-" + locale);
        },
    );
