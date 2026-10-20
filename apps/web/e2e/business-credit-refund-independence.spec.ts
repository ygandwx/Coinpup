import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessDraft } from "../src/business-draft-api";
import type { Account, Balance, ControlBalance } from "../src/ledger-api";
import type {
    BusinessPaymentReceipt,
    BusinessPaymentDetail,
    DocumentSettlementDetail,
} from "../src/business-settlement-api";
import type { AllocationReturnCapacity } from "../src/allocation-return-api";
import type { CreditCapacity, CreditSummary } from "../src/credit-api";
import type { RefundCreate, RefundSummary } from "../src/refund-api";
import { parseAmount } from "../src/money";

for (const first of ["credit", "refund"] as const)
    test(`fictional paid bill keeps ${first}-first credit and cash independent`, async ({
        page,
    }, info) => {
        test.setTimeout(120_000);
        const data = await confirmationSetup(page, "bill");
        const source = await api<BusinessDraft>(page, "POST", `${data.ledger}/business-documents`, {
            id: randomUUID(),
            document_kind: "bill",
            party_id: data.party.id,
            asset_id: "USD",
            issue_date: "2026-01-02",
            notes: "Fictional settled bill 中文",
            lines: [
                {
                    id: randomUUID(),
                    description: "Fictional fully paid service 中文",
                    quantity: "1",
                    unit_price: "100",
                    tax_rate_percent: "0",
                    category_id: data.source.lines[0].category_id,
                    recognition_date: "2026-01-02",
                },
            ],
        });
        const base = `${data.ledger}/business-documents/${source.id}`;
        await api(page, "POST", `${base}/confirm`, { id: randomUUID(), expected_version: 1 });
        const original = await api(page, "GET", `${base}/confirmation`);
        const bank = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
            name: "Fictional paid bill bank",
            kind: "bank",
            asset_ids: ["USD"],
        });
        const paid = await api<BusinessPaymentReceipt>(
            page,
            "POST",
            `${data.ledger}/business-payments`,
            {
                id: randomUUID(),
                direction: "payout",
                party_id: data.party.id,
                account_id: bank.id,
                asset_id: "USD",
                amount: "100",
                transaction_date: "2026-01-02",
                description: "Fictional one actual payment",
                initial_allocation: {
                    id: randomUUID(),
                    allocation_date: "2026-01-03",
                    lines: [
                        {
                            document_id: source.id,
                            document_line_id: source.lines[0].id,
                            payment_amount: "100",
                            document_amount: "100",
                        },
                    ],
                },
            },
        );
        const cash = async () => {
            const rows = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
            return rows.find((row) => row.account_id === bank.id && row.asset_id === "USD")!.amount;
        };
        expect(await cash()).toBe("-100.00");
        async function openCredit() {
            await page.goto(`/?entity=${data.entity.id}&view=business-credits`);
            await page.getByRole("button", { name: "Credit this document", exact: true }).click();
            await page.getByLabel("Credit transaction date", { exact: true }).fill("2026-01-05");
            await page
                .getByLabel("Credit reason", { exact: true })
                .fill("Fictional explicit net 20 and tax 0 中文");
            await page.getByRole("checkbox", { name: "Credit this line", exact: true }).check();
            await page.getByLabel("Credit net amount", { exact: true }).fill("20");
            await page.getByLabel("Credit tax amount", { exact: true }).fill("0");
            await page.getByLabel("Credit recognition date", { exact: true }).fill("2026-01-05");
            await page
                .getByRole("checkbox", {
                    name: "I reviewed the original classification",
                    exact: false,
                })
                .check();
        }
        await openCredit();
        let writes = 0;
        const count = (request: { method(): string; url(): string }) => {
            if (request.method() === "POST" && request.url().endsWith("/business-credits"))
                writes += 1;
        };
        page.on("request", count);
        await page.getByRole("button", { name: "Record credit", exact: true }).click();
        await expect(page.getByRole("alert")).toBeVisible();
        expect(writes).toBe(0);
        page.off("request", count);
        expect(await api(page, "GET", `${data.ledger}/business-credits`)).toEqual([]);
        expect(await cash()).toBe("-100.00");
        await page.getByRole("button", { name: "Cancel", exact: true }).click();
        const capacity = await api<AllocationReturnCapacity>(
            page,
            "GET",
            `${data.ledger}/business-allocations/${paid.initial_allocation!.id}/return-capacity`,
        );
        const returnId = randomUUID();
        await api(page, "POST", `${data.ledger}/business-allocation-returns`, {
            id: returnId,
            payment_id: paid.id,
            return_date: "2026-01-04",
            reason: "Fictional explicit release before credit",
            lines: [
                {
                    source_allocation_line_id: capacity.lines[0].source_allocation_line_id,
                    expected_version: 1,
                    payment_amount: "20",
                    document_amount: "20",
                },
            ],
        });
        expect(await cash()).toBe("-100.00");
        async function refund() {
            const body: RefundCreate = {
                id: randomUUID(),
                payment_id: paid.id,
                expected_version: 1,
                transaction_date: "2026-01-05",
                account_id: bank.id,
                asset_id: "USD",
                amount: "20",
                payment_amount: "20",
                reason: "Fictional actual refund independently received",
                allocation_return_ids: [returnId],
                fees: [],
            };
            return api(page, "POST", `${data.ledger}/business-refunds`, body);
        }
        if (first === "refund") {
            await refund();
            expect(await cash()).toBe("-80.00");
            const beforeCredit = await api<CreditCapacity>(page, "GET", `${base}/credit-capacity`);
            expect(beforeCredit.lines[0].credited_net_amount).toBe("0.00");
            expect(beforeCredit.lines[0].credited_tax_amount).toBe("0.00");
            expect(await api(page, "GET", `${data.ledger}/business-credits`)).toEqual([]);
        }
        await openCredit();
        await page.getByRole("button", { name: "Record credit", exact: true }).click();
        await expect(
            page.getByText(
                "The original credit request is saved; its complete line receipt is verified.",
                { exact: true },
            ),
        ).toBeVisible();
        expect(await cash()).toBe(first === "credit" ? "-100.00" : "-80.00");
        if (first === "credit") {
            expect(await api(page, "GET", `${data.ledger}/business-refunds`)).toEqual([]);
            await refund();
        }
        expect(await cash()).toBe("-80.00");
        const current = await api<CreditCapacity>(page, "GET", `${base}/credit-capacity`);
        expect(current.lines[0]).toMatchObject({
            category_id: source.lines[0].category_id,
            net_amount: "100.00",
            tax_amount: "0.00",
            credited_net_amount: "20.00",
            credited_tax_amount: "0.00",
            applied_amount: "80.00",
            available_gross_amount: "0.00",
        });
        const settled = await api<DocumentSettlementDetail>(page, "GET", `${base}/settlement`);
        expect(settled).toMatchObject({
            total_amount: "100.00",
            applied_amount: "80.00",
            outstanding_amount: "0.00",
        });
        expect(await api(page, "GET", `${base}/confirmation`)).toEqual(original);
        const payment = await api<BusinessPaymentDetail>(
            page,
            "GET",
            `${data.ledger}/business-payments/${paid.id}`,
        );
        expect(payment.receipt).toEqual(paid);
        expect(payment).toMatchObject({ applied_amount: "80.00", unapplied_amount: "0.00" });
        const credits = await api<CreditSummary[]>(page, "GET", `${data.ledger}/business-credits`);
        const refunds = await api<RefundSummary[]>(page, "GET", `${data.ledger}/business-refunds`);
        expect(credits).toHaveLength(1);
        expect(refunds).toHaveLength(1);
        const controls = await api<ControlBalance[]>(
            page,
            "GET",
            `${data.ledger}/control-balances?limit=100`,
        );
        for (const key of ["payable.supplier", "advance.paid"])
            expect(
                controls
                    .filter((row) => row.system_key === key)
                    .reduce((sum, row) => sum + parseAmount(row.amount, 2), 0n),
            ).toBe(0n);
        await page.getByRole("button", { name: "Back to credit list", exact: true }).click();
        await page.getByRole("button", { name: "View credit details", exact: true }).click();
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
                path: info.outputPath(`credit-${first}-independent-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    });
