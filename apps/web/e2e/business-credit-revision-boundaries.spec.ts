import { randomUUID } from "node:crypto";
import { expect, test, type Page, type Request, type Route } from "@playwright/test";
import { api } from "./helpers";
import {
    button,
    editReplacement,
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
    CreditDetail,
    CreditHistoryEntry,
    CreditReceipt,
} from "../src/credit-api";
import type {
    BusinessPaymentDetail,
    BusinessPaymentReceipt,
    BusinessAllocationReceipt,
} from "../src/business-settlement-api";
import type { Balance } from "../src/ledger-api";
import type { PeriodReceipt, PeriodState } from "../src/periods-api";

type Fixture = Awaited<ReturnType<typeof revisionSetup>>;
const matrix = [
    ["zh", 320],
    ["en", 375],
] as const;

async function snapshot(page: Page, data: Fixture) {
    const [current, original, confirmation, balances, history, capacity] = await Promise.all([
        api<CreditCurrent>(page, "GET", `${data.endpoint}/current`),
        api<CreditDetail>(page, "GET", data.endpoint),
        api(page, "GET", `${data.base}/confirmation`),
        api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`),
        api<CreditHistoryEntry[]>(page, "GET", `${data.endpoint}/history?limit=25&offset=0`),
        api<CreditCapacity>(page, "GET", `${data.base}/credit-capacity`),
    ]);
    expect(original).toEqual(data.detail);
    expect(confirmation).toEqual(data.confirmationDetail);
    return { current, original, confirmation, balances, history, capacity };
}

async function holdCorrection(page: Page, path: string) {
    let entered!: (request: Request) => void, release!: () => void;
    const entry = new Promise<Request>((resolve) => {
        entered = resolve;
    });
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    const handler = async (route: Route) => {
        if (route.request().method() !== "POST") return route.continue();
        entered(route.request());
        // The target has not reached the server: never wait while holding a DB lock.
        await gate;
        await route.continue();
    };
    const pattern = `**${path}`;
    await page.route(pattern, handler);
    return { entry, release, close: () => page.unroute(pattern, handler) };
}

function responseFor(page: Page, path: string) {
    return page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === path && response.request().method() === "POST",
    );
}

function expectedBody(data: Fixture, net: string, tax: string) {
    return {
        expected_version: 1,
        reason: revisionReason,
        replacement: {
            credit_date: "2026-01-06",
            reason: replacementReason,
            lines: data.source.lines.slice(0, 2).map((line) => ({
                source_line_id: line.id,
                net_amount: net,
                tax_amount: tax,
                recognition_date: "2026-01-06",
            })),
        },
    };
}

const cases = [
    {
        mode: "net",
        kind: "invoice",
        net: "18.00",
        tax: "2.77",
        otherNet: "82.77",
        otherTax: "0.00",
    },
    { mode: "tax", kind: "bill", net: "17.23", tax: "3.00", otherNet: "0.00", otherTax: "7.23" },
    { mode: "gross", kind: "bill", net: "35.00", tax: "5.00", otherNet: "0.00", otherTax: "0.00" },
] as const;

for (const config of cases)
    test(`fictional credit correction rejects independent ${config.mode} capacity competition`, async ({
        page,
    }, info) => {
        const data = await revisionSetup(page, config.kind);
        const path = `${data.endpoint}/corrections`;
        let payment: BusinessPaymentReceipt | null = null;
        if (config.mode === "gross") {
            payment = await api<BusinessPaymentReceipt>(
                page,
                "POST",
                `${data.ledger}/business-payments`,
                {
                    id: randomUUID(),
                    direction: "payout",
                    party_id: data.party.id,
                    account_id: data.balances[0].account_id,
                    asset_id: "USD",
                    amount: "80.00",
                    transaction_date: "2026-01-05",
                    description: "Fictional unallocated capacity payment 中文",
                },
            );
            expect(payment.initial_allocation).toBeNull();
            const detail = await api<BusinessPaymentDetail>(
                page,
                "GET",
                `${data.ledger}/business-payments/${payment.id}`,
            );
            expect(detail).toMatchObject({
                amount: "80.00",
                applied_amount: "0.00",
                unapplied_amount: "80.00",
            });
        }
        const beforeReview = await snapshot(page, data);
        expect(beforeReview.current.version).toBe(1);
        expect(beforeReview.balances).toHaveLength(1);
        expect(beforeReview.balances[0].amount).toBe(
            config.mode === "gross" ? "920.00" : "1000.00",
        );
        await openCurrent(page, data.entity.id);
        await editReplacement(page, ["A", "B"], config.net, config.tax);
        const held = await holdCorrection(page, path);
        try {
            const response = responseFor(page, path);
            await button(page, "Confirm correction").click();
            const request = await held.entry;
            expect(JSON.parse(request.postData()!)).toEqual(
                expectedBody(data, config.net, config.tax),
            );
            let otherPath: string;
            if (config.mode === "gross") {
                const allocation = await api<BusinessAllocationReceipt>(
                    page,
                    "POST",
                    `${data.ledger}/business-allocations`,
                    {
                        id: randomUUID(),
                        payment_id: payment!.id,
                        allocation_date: "2026-01-06",
                        lines: [
                            {
                                document_id: data.source.id,
                                document_line_id: data.source.lines[0].id,
                                payment_amount: "80.00",
                                document_amount: "80.00",
                            },
                        ],
                    },
                );
                otherPath = `${data.ledger}/business-payments/${payment!.id}`;
                expect(allocation.postings).toHaveLength(1);
                expect(await api<BusinessPaymentDetail>(page, "GET", otherPath)).toMatchObject({
                    applied_amount: "80.00",
                    unapplied_amount: "0.00",
                });
            } else {
                const other = await api<CreditReceipt>(
                    page,
                    "POST",
                    `${data.ledger}/business-credits`,
                    {
                        id: randomUUID(),
                        document_id: data.source.id,
                        expected_version: data.confirmation.document_version,
                        credit_date: "2026-01-06",
                        reason: `Fictional competing ${config.mode} only 中文`,
                        lines: [
                            {
                                source_line_id: data.source.lines[0].id,
                                net_amount: config.otherNet,
                                tax_amount: config.otherTax,
                                recognition_date: "2026-01-06",
                            },
                        ],
                    },
                );
                otherPath = `${data.ledger}/business-credits/${other.id}`;
                expect(other.postings).toHaveLength(1);
                expect(other.postings[0].source_line_id).toBe(data.source.lines[0].id);
            }
            // Capture after the legal competitor commits, while target forwarding is held.
            const beforeTarget = await snapshot(page, data);
            const otherBefore = await api(page, "GET", otherPath);
            expect(beforeTarget.current).toEqual(beforeReview.current);
            expect(beforeTarget.balances).toEqual(beforeReview.balances);
            const changed = beforeTarget.capacity.lines.find(
                (line) => line.source_line_id === data.source.lines[0].id,
            )!;
            expect(changed).toMatchObject(
                config.mode === "net"
                    ? {
                          credited_net_amount: "100.00",
                          credited_tax_amount: "2.77",
                          applied_amount: "0.00",
                          available_gross_amount: "7.23",
                      }
                    : config.mode === "tax"
                      ? {
                            credited_net_amount: "17.23",
                            credited_tax_amount: "10.00",
                            applied_amount: "0.00",
                            available_gross_amount: "82.77",
                        }
                      : {
                            credited_net_amount: "17.23",
                            credited_tax_amount: "2.77",
                            applied_amount: "80.00",
                            available_gross_amount: "10.00",
                        },
            );
            held.release();
            const rejected = await response;
            expect(rejected.status()).toBe(422);
            expect((await rejected.json()).detail.code).toBe(
                config.mode === "gross" ? "credit_exceeds_open" : "credit_exceeds_source",
            );
            await expect(button(page, "Edit rejected request")).toBeVisible();
            await expect(button(page, "Retry original request")).toHaveCount(0);
            await expect(button(page, "Close this revision receipt")).toHaveCount(0);
            // Public business facts and identities are unchanged by the rejected target.
            expect(await snapshot(page, data)).toEqual(beforeTarget);
            expect(await api(page, "GET", otherPath)).toEqual(otherBefore);
            expect(beforeTarget.history.map((row) => row.version)).toEqual([1]);
            await shots(page, info, `${config.mode}-rejection`, matrix, async (locale) => {
                const result = page.getByRole("region", {
                    name: locale === "zh" ? "本次贷项修订" : "This credit revision",
                    exact: true,
                });
                await expect(result.getByRole("alert")).toContainText(
                    config.mode === "gross"
                        ? locale === "zh"
                            ? "替代含税额超过当前可用额度"
                            : "The replacement gross exceeds available capacity"
                        : locale === "zh"
                          ? "替代净额或税额超过原行可用额度"
                          : "The replacement net or tax exceeds source capacity",
                );
                await expect(result).toContainText(revisionReason.trim());
            });
        } finally {
            held.release();
            await held.close();
        }
    });

test("fictional old credit period rejects a later replacement and explicit reopening restores reviewed UI submission", async ({
    page,
}, info) => {
    const data = await revisionSetup(page, "invoice");
    const path = `${data.endpoint}/corrections`;
    await openCurrent(page, data.entity.id);
    await editReplacement(page, ["A", "B"]);
    const period = await api<PeriodState>(page, "GET", `${data.ledger}/period`);
    expect(period.closed_through).toBeNull();
    const closed = await api<PeriodReceipt>(page, "POST", `${data.ledger}/period-changes`, {
        action: "close",
        expected_version: period.version,
        closed_through: "2026-01-05",
        reason: "Fictional old credit period closing 中文",
    });
    expect(closed.closed_through).toBe("2026-01-05");
    const before = await snapshot(page, data);
    expect(before.current).toMatchObject({ version: 1, credit_date: "2026-01-05" });
    expect(before.current.members.map((row) => row.recognition_date)).toEqual([
        "2026-01-05",
        "2026-01-05",
    ]);
    const response = responseFor(page, path);
    await button(page, "Confirm correction").click();
    const rejected = await response;
    expect(rejected.status()).toBe(422);
    expect((await rejected.json()).detail.code).toBe("period_closed");
    const originalRaw = rejected.request().postData()!,
        originalKey = rejected.request().headers()["idempotency-key"];
    expect(originalKey).toBeTruthy();
    expect(JSON.parse(originalRaw)).toEqual(expectedBody(data, "20.00", "3.00"));
    await expect(button(page, "Edit rejected request")).toBeVisible();
    await expect(button(page, "Retry original request")).toHaveCount(0);
    expect(await snapshot(page, data)).toEqual(before);
    await shots(page, info, "old-period-rejection", matrix, async (locale) => {
        await expect(page.getByRole("alert")).toContainText(
            locale === "zh"
                ? "不能只改新日期绕过原期间"
                : "changing only the new date does not release the original period",
        );
    });
    await button(page, "Edit rejected request").click();
    const form = page.getByRole("form", { name: "Correct credit group", exact: true });
    await expect(form).toBeVisible();
    await expect(
        form.getByLabel("Reason for this correction or cancellation", { exact: true }),
    ).toHaveValue(revisionReason);
    await expect(form.getByLabel("Replacement credit reason", { exact: true })).toHaveValue(
        replacementReason,
    );
    await expect(
        form.getByLabel("Replacement credit transaction date", { exact: true }),
    ).toHaveValue("2026-01-06");
    for (const letter of ["A", "B"]) {
        const row = form.getByRole("group", { name: lineName(letter), exact: true });
        await expect(row.getByRole("checkbox")).toBeChecked();
        await expect(row.getByLabel("Replacement net", { exact: true })).toHaveValue("20.00");
        await expect(row.getByLabel("Replacement tax", { exact: true })).toHaveValue("3.00");
        await expect(row.getByLabel("Replacement recognition date", { exact: true })).toHaveValue(
            "2026-01-06",
        );
    }
    await expect(
        form.getByRole("group", { name: lineName("C"), exact: true }).getByRole("checkbox"),
    ).not.toBeChecked();
    await expect(reviewed(page)).not.toBeChecked();
    // A known 422 has no successful receipt. Explicit review creates a new key, not an unknown-result retry.
    const latest = await api<PeriodState>(page, "GET", `${data.ledger}/period`);
    expect(latest.version).toBe(closed.version);
    const reason = "Fictional credit old-period correction approved 中文";
    const reopened = await api<PeriodReceipt>(page, "POST", `${data.ledger}/period-changes`, {
        action: "reopen",
        expected_version: latest.version,
        closed_through: null,
        reason,
    });
    expect(reopened).toMatchObject({
        action: "reopen",
        previous_closed_through: "2026-01-05",
        closed_through: null,
        reason,
    });
    const state = await api<PeriodState>(page, "GET", `${data.ledger}/period`);
    expect(state).toMatchObject({
        version: reopened.version,
        closed_through: null,
        last_reopened: reopened,
    });
    expect(
        await api<PeriodReceipt[]>(page, "GET", `${data.ledger}/period-changes?limit=25&offset=0`),
    ).toContainEqual(reopened);
    await expect(reviewed(page)).not.toBeChecked();
    await reviewed(page).check();
    const saved = await submitRevision(page, data.endpoint, "correct");
    expect(saved.raw).toBe(originalRaw);
    expect(saved.key).not.toBe(originalKey);
    expect(saved.receipt).toMatchObject({
        previous_version: 1,
        version: 2,
        credit_date: "2026-01-06",
    });
    const after = await snapshot(page, data);
    expect(after.current).toMatchObject({ version: 2, status: "active" });
    expect(after.history.map((row) => [row.version, row.action])).toEqual([
        [1, "create"],
        [2, "correct"],
    ]);
    expect(after.balances).toEqual(before.balances);
    expect(
        after.current.members.map((row) => [
            row.source_line_id,
            row.operation_version,
            row.net_amount,
            row.tax_amount,
        ]),
    ).toEqual(data.source.lines.slice(0, 2).map((line) => [line.id, 2, "20.00", "3.00"]));
    await shots(page, info, "reopened-success", matrix, async (locale) => {
        const result = page.getByRole("region", {
            name: locale === "zh" ? "本次贷项修订" : "This credit revision",
            exact: true,
        });
        await expect(result.getByRole("status")).toContainText(
            locale === "zh" ? "完整过渡回执已核对" : "complete transition receipt is verified",
        );
        await expect(result.getByRole("alert")).toHaveCount(0);
    });
});
