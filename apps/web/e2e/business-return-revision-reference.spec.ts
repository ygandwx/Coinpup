import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { openReturnVersions, returnRevisionFixture } from "./return-revision-helpers";
import { openRefundVersions } from "./refund-revision-helpers";
import type { components } from "../src/generated/openapi";
type Wire = components["schemas"];

for (const locale of ["zh", "en"] as const)
    test(
        "fictional " +
            locale +
            " moves active refund evidence explicitly before cancelling its referenced return",
        async ({ page }, info) => {
            const en = locale === "en",
                f = await returnRevisionFixture(page);
            const other = await api<Wire["BusinessAllocationReturnResponse"]>(
                page,
                "POST",
                f.ledger + "/business-allocation-returns",
                {
                    id: randomUUID(),
                    payment_id: f.payment.id,
                    return_date: "2026-01-04",
                    reason: "Fictional other evidence",
                    lines: [f.lines[1]],
                },
            );
            const refund = await api<Wire["BusinessRefundResponse"]>(
                page,
                "POST",
                f.ledger + "/business-refunds",
                {
                    id: randomUUID(),
                    payment_id: f.payment.id,
                    expected_version: 1,
                    account_id: f.bank.id,
                    asset_id: "USD",
                    amount: "5.00",
                    payment_amount: "5.00",
                    transaction_date: "2026-01-06",
                    reason: "Fictional explicit provenance",
                    fees: [],
                    allocation_return_ids: [f.original.id],
                },
            );
            const ref = f.ledger + "/business-refunds/" + refund.id;
            const switchLanguage = (toEnglish: boolean) =>
                page
                    .getByRole("button", { name: toEnglish ? "English" : "中文", exact: true })
                    .click();
            async function cancel() {
                const versions = await openReturnVersions(
                    page,
                    f.entity.id,
                    locale,
                    "Fictional original return 中文",
                );
                await versions
                    .getByRole("button", {
                        name: en ? "Cancel this return group" : "取消此组退回",
                        exact: true,
                    })
                    .click();
                const form = versions.getByRole("form", {
                    name: en ? "Return cancellation" : "退回取消",
                });
                await form
                    .getByLabel(en ? "Revision reason" : "修订原因", { exact: true })
                    .fill("Fictional evidence checked");
                await form.getByRole("checkbox").check();
                await switchLanguage(!en);
                const translated = page.getByRole("form", {
                    name: en ? "退回取消" : "Return cancellation",
                });
                await expect(
                    translated.getByRole("textbox", {
                        name: en ? "修订原因" : "Revision reason",
                        exact: true,
                    }),
                ).toHaveValue("Fictional evidence checked");
                await expect(translated.getByRole("checkbox")).toBeChecked();
                await switchLanguage(en);
                await expect(form.getByRole("checkbox")).toBeChecked();
                const received = page.waitForResponse(
                    (r) =>
                        r.url().endsWith(f.path + "/cancellations") &&
                        r.request().method() === "POST",
                );
                await form
                    .getByRole("button", {
                        name: en ? "Submit return cancellation" : "提交退回取消",
                    })
                    .click();
                return received;
            }
            const rejected = await cancel();
            expect(rejected.status()).toBe(409);
            expect((await rejected.json()).detail.code).toBe("allocation_return_refund_reference");
            const result = page.locator(".settlement-result");
            await expect(result.getByRole("alert")).toContainText(
                en ? "explicitly update their evidence" : "明确更新其证据",
            );
            for (const width of info.project.name === "mobile-chromium" ? [375, 320] : [1440]) {
                await page.setViewportSize({ width, height: 820 });
                await expectNoOverflow(page);
                const name = "return-reference-" + locale + "-" + width,
                    path = info.outputPath(name + ".png");
                await result.screenshot({ path });
                await info.attach(name, { path, contentType: "image/png" });
            }
            expect(
                (await api<Wire["AllocationReturnCurrent"]>(page, "GET", f.path + "/current"))
                    .version,
            ).toBe(1);
            const before = await api<Wire["RefundCurrent"]>(page, "GET", ref + "/current");
            expect(before.fact?.allocation_return_ids).toEqual([f.original.id]);
            expect(before.version).toBe(1);
            await result
                .getByRole("button", { name: en ? "Back to settlements" : "返回收付列表" })
                .click();
            const refunds = await openRefundVersions(page, f.entity.id, locale);
            await refunds
                .getByRole("button", {
                    name: en ? "Correct this refund" : "更正此笔退款",
                    exact: true,
                })
                .click();
            const editor = refunds.getByRole("form", {
                name: en ? "Correct refund facts" : "更正退款事实",
            });
            await editor
                .getByLabel(en ? "Correction reason" : "更正原因", { exact: true })
                .fill("Fictional move evidence explicitly");
            const old = editor.getByRole("checkbox", { name: new RegExp(f.original.id) });
            await expect(old).toBeChecked();
            await old.uncheck();
            await editor.getByRole("checkbox", { name: new RegExp(other.id) }).check();
            await editor
                .getByRole("checkbox", {
                    name: en ? /I reviewed original principal/ : /已核对原本金/,
                })
                .check();
            await switchLanguage(!en);
            const translated = page.getByRole("form", {
                name: en ? "更正退款事实" : "Correct refund facts",
            });
            await expect(
                translated.getByRole("textbox", {
                    name: en ? "更正原因" : "Correction reason",
                    exact: true,
                }),
            ).toHaveValue("Fictional move evidence explicitly");
            await expect(
                translated.getByRole("checkbox", { name: new RegExp(f.original.id) }),
            ).not.toBeChecked();
            await expect(
                translated.getByRole("checkbox", { name: new RegExp(other.id) }),
            ).toBeChecked();
            await expect(
                translated.getByRole("checkbox", {
                    name: en ? /已核对原本金/ : /I reviewed original principal/,
                }),
            ).toBeChecked();
            await switchLanguage(en);
            await expect(old).not.toBeChecked();
            await expect(
                editor.getByRole("checkbox", { name: new RegExp(other.id) }),
            ).toBeChecked();
            await editor
                .getByRole("button", {
                    name: en ? "Confirm refund correction" : "确认更正退款",
                    exact: true,
                })
                .click();
            await expect(result.getByRole("status")).toContainText(
                en ? "Verified saved receipt" : "已核对固定回执",
            );
            const after = await api<Wire["RefundCurrent"]>(page, "GET", ref + "/current");
            expect(after.version).toBe(2);
            expect(after.fact).toEqual({ ...before.fact, allocation_return_ids: [other.id] });
            const proof = await api<Wire["RefundRevisionResponse"]>(
                page,
                "GET",
                ref + "/revisions/" + after.revision_id,
            );
            expect(proof.previous.allocation_return_ids).toEqual([f.original.id]);
            expect(proof.replacement?.allocation_return_ids).toEqual([other.id]);
            await result
                .getByRole("button", { name: en ? "Back to settlements" : "返回收付列表" })
                .click();
            expect((await cancel()).status()).toBe(200);
            expect(await api(page, "GET", f.path + "/current")).toMatchObject({
                status: "cancelled",
                version: 2,
            });
            expect(await api(page, "GET", ref + "/current")).toEqual(after);
            expect(await api(page, "GET", ref + "/revisions/" + after.revision_id)).toEqual(proof);
        },
    );
