import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import { openRefundVersions } from "./refund-revision-helpers";
import type { Account } from "../src/ledger-api";
import type { BusinessPaymentReceipt } from "../src/business-settlement-api";
import type { AllocationReturnCapacity } from "../src/allocation-return-api";
import type {
    RefundCurrent,
    RefundDetail,
    RefundReceipt,
    RefundRevisionReceipt,
} from "../src/refund-api";

test("explicit changes to ordered return evidence preserve both original and historical refund versions", async ({
    page,
}, info) => {
    const f = await confirmationSetup(page);
    await api(page, "POST", f.base + "/confirm", { id: randomUUID(), expected_version: 1 });
    const bank = await api<Account>(page, "POST", f.ledger + "/accounts", {
        kind: "bank",
        name: "Fictional evidence bank",
        asset_ids: ["USD"],
    });
    const paid = await api<BusinessPaymentReceipt>(page, "POST", f.ledger + "/business-payments", {
        id: randomUUID(),
        direction: "receipt",
        party_id: f.party.id,
        account_id: bank.id,
        asset_id: "USD",
        amount: "100",
        transaction_date: "2026-01-02",
        description: "Fictional applied funds",
        initial_allocation: {
            id: randomUUID(),
            allocation_date: "2026-01-03",
            lines: [
                {
                    document_id: f.source.id,
                    document_line_id: f.source.lines[0].id,
                    payment_amount: "100",
                    document_amount: "100",
                },
            ],
        },
    });
    const references: string[] = [];
    for (const text of ["first", "second", "third"]) {
        const capacity = await api<AllocationReturnCapacity>(
            page,
            "GET",
            f.ledger + "/business-allocations/" + paid.initial_allocation!.id + "/return-capacity",
        );
        const line = capacity.lines[0],
            id = randomUUID();
        await api(page, "POST", f.ledger + "/business-allocation-returns", {
            id,
            payment_id: paid.id,
            return_date: "2026-01-04",
            reason: "Fictional " + text + " return",
            lines: [
                {
                    source_allocation_line_id: line.source_allocation_line_id,
                    expected_version: line.expected_version,
                    payment_amount: "10.00",
                    document_amount: "10.00",
                },
            ],
        });
        references.push(id);
    }
    const original = await api<RefundReceipt>(page, "POST", f.ledger + "/business-refunds", {
        id: randomUUID(),
        payment_id: paid.id,
        expected_version: 1,
        account_id: bank.id,
        asset_id: "USD",
        amount: "20",
        payment_amount: "20",
        transaction_date: "2026-01-05",
        reason: "Fictional cash with three proofs",
        allocation_return_ids: references,
    });
    const path = f.ledger + "/business-refunds/" + original.id;
    const versions = await openRefundVersions(page, f.entity.id, "en");
    await versions.getByRole("button", { name: "Correct this refund", exact: true }).click();
    const form = versions.getByRole("form", { name: "Correct refund facts" });
    await form.getByText("Edit return evidence in its current order", { exact: true }).click();
    const order = form
        .locator("details")
        .filter({ hasText: "Edit return evidence in its current order" })
        .getByRole("listitem");
    await expect(order).toHaveCount(3);
    for (let i = 0; i < 3; i++) await expect(order.nth(i)).toContainText(references[i]);
    await order.nth(2).getByRole("button", { name: "Move up", exact: true }).click();
    await order.nth(0).getByRole("button", { name: "Remove this reference", exact: true }).click();
    await expect(order).toHaveCount(2);
    await expect(order.nth(0)).toContainText(references[2]);
    await expect(order.nth(1)).toContainText(references[1]);
    await form
        .getByLabel("Correction reason", { exact: true })
        .fill("Fictional explicit evidence correction");
    await form.getByRole("checkbox", { name: /I reviewed original principal/ }).check();
    const result = page.waitForResponse(
        (r) => r.url().endsWith(path + "/corrections") && r.request().method() === "POST",
    );
    await form.getByRole("button", { name: "Confirm refund correction", exact: true }).click();
    const response = await result;
    expect(response.status()).toBe(200);
    const receipt = (await response.json()) as RefundRevisionReceipt;
    expect(receipt.previous.allocation_return_ids).toEqual(references);
    expect(receipt.replacement!.allocation_return_ids).toEqual([references[2], references[1]]);
    const current = await api<RefundCurrent>(page, "GET", path + "/current");
    expect(current.fact?.allocation_return_ids).toEqual([references[2], references[1]]);
    expect(current.fact?.payment_amount).toBe("20.00");
    expect((await api<RefundDetail>(page, "GET", path)).receipt).toEqual(original);
    expect(await api(page, "GET", path + "/revisions/" + receipt.revision_id)).toEqual(receipt);
    await expectNoOverflow(page);
    await info.attach("refund-evidence-order", {
        body: JSON.stringify({
            original: references,
            current: current.fact!.allocation_return_ids,
        }),
        contentType: "application/json",
    });
});
