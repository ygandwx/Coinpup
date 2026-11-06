import { expect, test } from "@playwright/test";
import { api } from "./helpers";
import {
    button,
    closeReceipt,
    editReplacement,
    fixedPost,
    historyDetail,
    lineName,
    openCurrent,
    replacementReason,
    reviewed,
    revisionReason,
    revisionSetup,
    shots,
    submitRevision,
} from "./credit-revision-flow-helpers";
import type {
    CreditCapacity,
    CreditCurrent,
    CreditHistoryEntry,
    CreditRevisionReceipt,
} from "../src/credit-api";

const fullMatrix = [
    ["zh", 1440],
    ["en", 1440],
    ["zh", 375],
    ["en", 375],
    ["zh", 320],
    ["en", 320],
] as const;
const narrowMatrix = [
    ["zh", 320],
    ["en", 375],
] as const;

for (const kind of ["invoice", "bill"] as const)
    test(`fictional ${kind} complete credit A/B to A/C to B/C and cancellation`, async ({
        page,
    }, info) => {
        const data = await revisionSetup(page, kind);
        const [a, b, c] = data.source.lines.map((line) => line.id);
        const [firstA, firstB] = data.original.postings;
        expect(data.confirmation.document_version).toBe(data.source.version + 1);
        expect(data.confirmation.document_version).not.toBe(1);
        const current = () => api<CreditCurrent>(page, "GET", `${data.endpoint}/current`);
        const invariant = async () => {
            expect(await api(page, "GET", data.endpoint)).toEqual(data.detail);
            expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(
                data.confirmationDetail,
            );
            expect(await api(page, "GET", `${data.ledger}/balances?limit=100`)).toEqual(
                data.balances,
            );
        };
        const capacity = async (selected: string[]) => {
            const result = await api<CreditCapacity>(page, "GET", `${data.base}/credit-capacity`);
            expect(result.lines).toHaveLength(3);
            for (const line of result.lines) {
                const included = selected.includes(line.source_line_id);
                expect(line).toMatchObject({
                    credited_net_amount: included ? "20.00" : "0.00",
                    credited_tax_amount: included ? "3.00" : "0.00",
                    applied_amount: "0.00",
                    remaining_net_amount: included ? "80.00" : "100.00",
                    remaining_tax_amount: included ? "7.00" : "10.00",
                    available_gross_amount: included ? "87.00" : "110.00",
                });
            }
        };
        await openCurrent(page, data.entity.id);
        expect((await current()).version).toBe(1);
        await editReplacement(page, ["A", "C"]);
        await expect(
            page.getByRole("region", { name: "Current credit facts", exact: true }),
        ).toContainText("Current group version: 1");
        await shots(
            page,
            info,
            `${kind}-complete-editor`,
            kind === "invoice" ? fullMatrix : narrowMatrix,
            async (locale) => {
                const reason = page.getByLabel(
                    locale === "zh"
                        ? "本次更正或取消的原因"
                        : "Reason for this correction or cancellation",
                    { exact: true },
                );
                await expect(reason).toHaveValue(revisionReason);
                await expect(reviewed(page, locale)).toBeChecked();
                for (const letter of ["A", "C"]) {
                    const row = page.getByRole("group", { name: lineName(letter), exact: true });
                    await expect(
                        row.getByLabel(locale === "zh" ? "替代净额" : "Replacement net", {
                            exact: true,
                        }),
                    ).toHaveValue("20.00");
                    await expect(
                        row.getByLabel(locale === "zh" ? "替代税额" : "Replacement tax", {
                            exact: true,
                        }),
                    ).toHaveValue("3.00");
                }
                await expect(
                    page
                        .getByRole("group", { name: lineName("B"), exact: true })
                        .getByRole("checkbox"),
                ).not.toBeChecked();
                await expect(
                    button(page, locale === "zh" ? "确认更正" : "Confirm correction"),
                ).toBeEnabled();
            },
        );
        const second = await submitRevision(page, data.endpoint, "correct");
        expect(JSON.parse(second.raw)).toEqual({
            expected_version: 1,
            reason: revisionReason,
            replacement: {
                credit_date: "2026-01-06",
                reason: replacementReason,
                lines: [a, c].map((id) => ({
                    source_line_id: id,
                    net_amount: "20.00",
                    tax_amount: "3.00",
                    recognition_date: "2026-01-06",
                })),
            },
        });
        expect(second.receipt).toMatchObject({
            version: 2,
            previous_version: 1,
            reason: revisionReason.trim(),
        });
        expect(
            second.receipt.members.map((row) => [
                row.source_line_id,
                row.action,
                row.operation_version,
            ]),
        ).toEqual([
            [a, "correct", 2],
            [c, "create", 1],
            [b, "cancel", 2],
        ]);
        expect(second.receipt.members[0]).toMatchObject({
            operation_id: firstA.operation_id,
            previous_journal_id: firstA.journal_id,
            previous_net_amount: firstA.net_amount,
            previous_tax_amount: firstA.tax_amount,
        });
        expect(second.receipt.members[2]).toMatchObject({
            operation_id: firstB.operation_id,
            previous_journal_id: firstB.journal_id,
            journal_id: null,
            net_amount: null,
            tax_amount: null,
        });
        expect(second.receipt.members[1].operation_id).not.toBe(
            second.receipt.members[1].source_operation_id,
        );
        await shots(
            page,
            info,
            `${kind}-mixed-receipt`,
            kind === "invoice" ? fullMatrix : narrowMatrix,
            async (locale) => {
                const result = page.getByRole("region", {
                    name: locale === "zh" ? "本次贷项修订" : "This credit revision",
                    exact: true,
                });
                await expect(result.getByRole("heading", { level: 4 })).toHaveCount(3);
                await expect(result).toContainText(
                    locale === "zh" ? "保留并更正" : "Retained and corrected",
                );
                await expect(result).toContainText(locale === "zh" ? "移除" : "Removed");
                await expect(result).toContainText(locale === "zh" ? "新增" : "Added");
                await expect(result).toContainText("20.000000000000000000 USD");
                await expect(result).toContainText("3.000000000000000000 USD");
                await expect(result).toContainText(
                    locale === "zh"
                        ? "此回执不代表今日的当前状态"
                        : "This receipt does not represent today's current state.",
                );
            },
        );
        await invariant();
        await capacity([a, c]);
        await closeReceipt(page);
        await editReplacement(page, ["B", "C"]);
        const third = await submitRevision(page, data.endpoint, "correct");
        expect(third.receipt).toMatchObject({ version: 3, previous_version: 2 });
        expect(
            third.receipt.members.map((row) => [
                row.source_line_id,
                row.action,
                row.operation_version,
            ]),
        ).toEqual([
            [b, "create", 1],
            [c, "correct", 2],
            [a, "cancel", 3],
        ]);
        expect(third.receipt.members[0].operation_id).not.toBe(firstB.operation_id);
        expect(third.receipt.members[0].operation_id).not.toBe(
            third.receipt.members[0].source_operation_id,
        );
        expect(third.receipt.members[1]).toMatchObject({
            operation_id: second.receipt.members[1].operation_id,
            previous_journal_id: second.receipt.members[1].journal_id,
        });
        const live = await current();
        expect(
            live.members.map((row) => [
                row.source_line_id,
                row.operation_id,
                row.operation_version,
            ]),
        ).toEqual(
            third.receipt.members
                .slice(0, 2)
                .map((row) => [row.source_line_id, row.operation_id, row.operation_version]),
        );
        await invariant();
        await capacity([b, c]);
        await closeReceipt(page);
        await button(page, "Cancel complete credit group").click();
        await page
            .getByLabel("Reason for this correction or cancellation", { exact: true })
            .fill("Fictional complete cancellation 中文");
        await reviewed(page).check();
        await shots(page, info, `${kind}-cancel-review`, narrowMatrix, async (locale) => {
            const form = page.getByRole("form", {
                name: locale === "zh" ? "取消贷项组" : "Cancel credit group",
                exact: true,
            });
            await expect(form).toBeVisible();
            await expect(reviewed(page, locale)).toBeChecked();
            await expect(
                form.getByLabel(locale === "zh" ? "替代净额" : "Replacement net", { exact: true }),
            ).toHaveCount(0);
        });
        const fourth = await submitRevision(page, data.endpoint, "cancel");
        expect(fourth.receipt).toMatchObject({
            version: 4,
            previous_version: 3,
            credit_date: null,
            replacement_reason: null,
        });
        expect(
            fourth.receipt.members.map((row) => [
                row.source_line_id,
                row.action,
                row.operation_version,
            ]),
        ).toEqual([
            [b, "cancel", 2],
            [c, "cancel", 3],
        ]);
        for (const row of fourth.receipt.members)
            expect(row).toMatchObject({
                line_no: null,
                journal_id: null,
                net_amount: null,
                tax_amount: null,
                total_amount: null,
                recognition_date: null,
                previous_net_amount: "20.000000000000000000",
                previous_tax_amount: "3.000000000000000000",
                previous_total_amount: "23.000000000000000000",
            });
        const journals = [second, third, fourth].flatMap((step) =>
            step.receipt.members.flatMap((row) =>
                [row.reversal_journal_id, row.journal_id].filter((id): id is string => id !== null),
            ),
        );
        expect(new Set(journals).size).toBe(journals.length);
        for (const row of data.original.postings) expect(journals).not.toContain(row.journal_id);
        expect(await current()).toMatchObject({
            version: 4,
            status: "cancelled",
            members: [],
            credit_date: null,
            credit_reason: null,
        });
        await invariant();
        await capacity([]);
        await closeReceipt(page);
        await expect(
            page.getByText("Cancelled; cannot be reactivated", { exact: false }),
        ).toBeVisible();
        await expect(button(page, "Correct complete credit group")).toHaveCount(0);
        await expect(button(page, "Cancel complete credit group")).toHaveCount(0);
        await button(page, "View version history").click();
        const directory = await api<CreditHistoryEntry[]>(
            page,
            "GET",
            `${data.endpoint}/history?limit=25&offset=0`,
        );
        expect(directory.map((row) => [row.version, row.action, row.revision_id])).toEqual([
            [1, "create", null],
            [2, "correct", second.receipt.revision_id],
            [3, "correct", third.receipt.revision_id],
            [4, "cancel", fourth.receipt.revision_id],
        ]);
        const original = await historyDetail(page, 1);
        await expect(original).toContainText(data.detail.reason);
        await expect(original.locator(".master-records > li")).toHaveCount(2);
        await shots(page, info, `${kind}-original-history`, [["en", 1440]]);
        await button(page, "Back to versions").click();
        const fixed = await historyDetail(page, 2);
        await expect(fixed.getByText(second.receipt.revision_id, { exact: true })).toBeVisible();
        await expect(fixed.locator(".master-records > li")).toHaveCount(3);
        await shots(page, info, `${kind}-intermediate-history`, [["zh", 375]]);
        await button(page, "Back to versions").click();
        const cancelled = await historyDetail(page, 4);
        await expect(cancelled.locator(".master-records > li")).toHaveCount(2);
        await shots(page, info, `${kind}-cancel-history`, narrowMatrix, async (locale) => {
            const view = page.getByRole("region", {
                name: locale === "zh" ? "贷项版本历史" : "Credit version history",
                exact: true,
            });
            await expect(view).toContainText(
                locale === "zh" ? "不能确认未知提交" : "proof of an unknown submission",
            );
            await expect(
                view.getByText(locale === "zh" ? "移除成员" : "Removed member", { exact: true }),
            ).toHaveCount(2);
        });
        // Fixed reads and same-key retries remain the exact historical JSON after cancellation.
        expect(await fixedPost(page, data.path, data.raw, data.key, 201)).toEqual(data.original);
        for (const step of [second, third, fourth]) {
            expect(
                await api(page, "GET", `${data.endpoint}/revisions/${step.receipt.revision_id}`),
            ).toEqual(step.receipt);
            expect(
                await fixedPost<CreditRevisionReceipt>(page, step.path, step.raw, step.key, 200),
            ).toEqual(step.receipt);
        }
        await invariant();
        await capacity([]);
        expect(
            await api<CreditHistoryEntry[]>(
                page,
                "GET",
                `${data.endpoint}/history?limit=25&offset=0`,
            ),
        ).toEqual(directory);
        await button(page, "Back to current state").click();
        await expect(page.getByText("Group version: 4", { exact: false })).toBeVisible();
        expect((await current()).members).toEqual([]);
    });

test("fictional long credit replacement preserves integer precision in both languages", async ({
    page,
}, info) => {
    const data = await revisionSetup(page, "invoice", true);
    await openCurrent(page, data.entity.id);
    const amount = "70000000000000000000.01";
    await editReplacement(page, ["A"], amount, "0.00");
    await shots(
        page,
        info,
        "long-editor",
        [
            ["zh", 375],
            ["en", 320],
        ],
        async (locale) => {
            await expect(
                page.getByLabel(locale === "zh" ? "替代净额" : "Replacement net", { exact: true }),
            ).toHaveValue(amount);
            await expect(reviewed(page, locale)).toBeChecked();
        },
    );
    const saved = await submitRevision(page, data.endpoint, "correct");
    expect(saved.receipt.members[0]).toMatchObject({
        net_amount: "70000000000000000000.010000000000000000",
        tax_amount: "0.000000000000000000",
        total_amount: "70000000000000000000.010000000000000000",
    });
    await shots(
        page,
        info,
        "long-receipt",
        [
            ["zh", 375],
            ["en", 320],
        ],
        async () => {
            await expect(
                page.getByRole("region", { name: /本次贷项修订|This credit revision/u }),
            ).toContainText("70,000,000,000,000,000,000.010000000000000000 USD");
        },
    );
    const current = await api<CreditCurrent>(page, "GET", `${data.endpoint}/current`);
    expect(current.members[0]).toMatchObject({
        net_amount: amount,
        tax_amount: "0.00",
        total_amount: amount,
    });
    expect(await api(page, "GET", `${data.ledger}/balances?limit=100`)).toEqual(data.balances);
    expect(await api(page, "GET", data.endpoint)).toEqual(data.detail);
});
