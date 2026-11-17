import { test, expect } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { returnRevisionFixture, openReturnVersions } from "./return-revision-helpers";
import type { components } from "../src/generated/openapi";
type Receipt = components["schemas"]["AllocationReturnRevisionResponse"];

for (const locale of ["zh", "en"] as const)
    test(`fictional ${locale} reads a retired return and its exact replacement history`, async ({
        page,
    }, info) => {
        const data = await returnRevisionFixture(page);
        const edited = await api<Receipt>(page, "POST", data.path + "/corrections", {
            expected_version: 1,
            reason: "Fictional swapped source 中文",
            replacement: {
                return_date: "2026-01-05",
                reason: "Fictional active replacement 中文",
                lines: [data.lines[1]],
            },
        });
        await api(page, "POST", data.path + "/cancellations", {
            expected_version: 2,
            reason: "Fictional terminal return 中文",
        });
        const en = locale === "en";
        let writes = 0;
        page.on("request", (r) => {
            if (r.method() === "POST" && r.url().includes("/business-allocation-returns")) writes++;
        });
        const panel = await openReturnVersions(page, data.entity.id, locale);
        await expect(
            panel.getByRole("heading", {
                name: en ? "Group version 3 · Cancelled" : "组版本 3 · 已取消",
            }),
        ).toBeVisible();
        const current = data.path + "/current";
        const currentButton = panel.getByRole("button", {
            name: en ? "Current return group" : "当前退回组",
            exact: true,
        });
        await page.route(
            "**" + current,
            async (route) => {
                const response = await route.fetch();
                expect(response.status()).toBe(200);
                const body = await response.json();
                expect(body.return_id).toBe(data.original.id);
                await route.fulfill({ response, json: { ...body, return_id: data.payment.id } });
            },
            { times: 1 },
        );
        await currentButton.click();
        await expect(panel.getByRole("alert")).toBeVisible();
        const version = panel.getByRole("heading", {
            name: en ? "Group version 3 · Cancelled" : "组版本 3 · 已取消",
        });
        await expect(version).toHaveCount(0);
        await currentButton.click();
        await expect(version).toBeVisible();
        await expect(panel.getByRole("alert")).toHaveCount(0);
        await panel
            .getByRole("button", {
                name: en ? "Return version history" : "退回版本目录",
                exact: true,
            })
            .click();
        await expect(panel.getByRole("listitem")).toHaveCount(3);
        await panel
            .getByRole("button", { name: en ? "View version 2" : "查看版本 2", exact: true })
            .click();
        await expect(
            panel.getByRole("heading", {
                name: en ? "Saved revision receipt · 1 → 2" : "固定修订回执 · 1 → 2",
            }),
        ).toBeVisible();
        const rows = panel.getByRole("listitem");
        await expect(rows).toHaveCount(2);
        await expect(rows.nth(0)).toContainText(en ? "Added source" : "新加入来源");
        await expect(rows.nth(1)).toContainText(en ? "Removed source" : "移除来源");
        await expect(rows.nth(0)).toContainText(data.lines[1].source_allocation_line_id);
        await expect(rows.nth(1)).toContainText(data.lines[0].source_allocation_line_id);
        const normal = edited.members[0].replacement!,
            old = edited.members[1].previous!;
        await rows.nth(1).locator("summary").click();
        await expect(rows.nth(1)).toContainText(old.journal_id);
        await expect(rows.nth(1)).toContainText(edited.members[1].reversal_journal_id!);
        expect(normal.payment_amount).toBe("11.000000000000000000");
        expect(normal.document_amount).toBe("10.000000000000000000");
        await expect(rows.nth(0)).toContainText("11.00 USD / 10.00 EUR");
        await expect(rows.nth(1)).toContainText("11.00 USD / 10.00 EUR");
        for (const width of info.project.name === "mobile-chromium" ? [375, 320] : [1440]) {
            await page.setViewportSize({ width, height: width === 1440 ? 1000 : 800 });
            await expectNoOverflow(page);
            const name = `return-history-${locale}-${width}`,
                path = info.outputPath(name + ".png");
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
        expect(writes).toBe(0);
    });
