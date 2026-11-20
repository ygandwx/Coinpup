import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessDraft } from "../src/business-draft-api";
import type { DocumentCurrent, DocumentRevisionReceipt } from "../src/document-revision-api";
const button = (p: Page, name: string) => p.getByRole("button", { name, exact: true });
async function open(page: Page, action = "correct") {
    await button(page, "Confirmed documents").click();
    await button(page, "Current state, correction and cancellation").click();
    await button(
        page,
        action === "correct" ? "Correct whole document" : "Cancel whole document",
    ).click();
}
const reviewed = (page: Page) =>
    page.getByRole("checkbox", { name: "I reviewed the current document", exact: false });
const reply = (page: Page, path: string) =>
    page.waitForResponse(
        (r) => r.request().method() === "POST" && new URL(r.url()).pathname === path,
    );
test("fictional full200positive-source editor submits all200mappings and cancels all200current accruals", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await confirmationSetup(page, "bill");
    const source = await api<BusinessDraft>(page, "PUT", data.base, {
        expected_version: data.source.version,
        document_kind: "bill",
        party_id: data.party.id,
        asset_id: "USD",
        issue_date: "2026-01-02",
        due_date: null,
        notes: "Fictional all200positive rows",
        lines: Array.from({ length: 200 }, (_, i) => ({
            id: randomUUID(),
            description: `Fictional positive source ${i} 中文`,
            quantity: "1.00",
            unit_price: "100.00",
            discount_amount: "0",
            tax_rate_percent: "10",
            category_id: data.source.lines[0].category_id,
            recognition_date: "2026-01-02",
        })),
    });
    await api(page, "POST", `${data.base}/confirm`, {
        id: randomUUID(),
        expected_version: source.version,
    });
    const original = await api(page, "GET", `${data.base}/confirmation`);
    await open(page);
    await expect(page.locator(".draft-line")).toHaveCount(200);
    await expect(page.getByTestId("document-current-line")).toHaveCount(200);
    const reason = "  Fictional complete200 reviewed 中文  ";
    await page.getByLabel("Reason (required)", { exact: true }).fill(reason);
    await page.getByLabel("Unit price before tax", { exact: true }).last().fill("120.00");
    await reviewed(page).check();
    for (const locale of ["zh", "en"]) {
        await page.setViewportSize({ width: 320, height: 1000 });
        await button(page, locale === "zh" ? "中文" : "English").click();
        await expect(
            page.getByLabel(locale === "zh" ? "原因（必填）" : "Reason (required)", {
                exact: true,
            }),
        ).toHaveValue(reason);
        const last = page
            .getByLabel(locale === "zh" ? "未税单价" : "Unit price before tax", { exact: true })
            .last();
        await expect(last).toHaveValue("120.00");
        await last.scrollIntoViewIfNeeded();
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`document-editor200-last-${locale}-320.png`),
        });
    }
    const r = reply(page, `${data.base}/corrections`);
    await button(page, "Submit whole-document correction").click();
    const response = await r;
    expect(response.status(), await response.text()).toBe(200);
    const receipt = (await response.json()) as DocumentRevisionReceipt;
    expect(receipt.previous.lines).toHaveLength(200);
    expect(receipt.replacement!.lines).toHaveLength(200);
    expect(receipt.reversals).toHaveLength(200);
    expect(
        JSON.parse(response.request().postData()!).replacement.lines.map(
            (v: { id: string }) => v.id,
        ),
    ).toEqual(source.lines.map((v) => v.id));
    expect(receipt.replacement!.lines[199].unit_price).toBe("120.00");
    await expect(
        page.getByText("The complete receipt for this revision is verified.", { exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("document-version-line")).toHaveCount(400);
    await button(page, "Close").click();
    await button(page, "Current state, correction and cancellation").click();
    await button(page, "Cancel whole document").click();
    await page.getByLabel("Reason (required)", { exact: true }).fill("Fictional cancel all200");
    await reviewed(page).check();
    const c = reply(page, `${data.base}/cancellations`);
    await button(page, "Submit whole-document cancellation").click();
    const cancelled = await c;
    expect(cancelled.status(), await cancelled.text()).toBe(200);
    const terminal = (await cancelled.json()) as DocumentRevisionReceipt;
    expect(terminal.reversals).toHaveLength(200);
    expect(terminal.replacement).toBeNull();
    await expect(
        page.getByText("The complete receipt for this revision is verified.", { exact: true }),
    ).toBeVisible();
    expect((await api<DocumentCurrent>(page, "GET", `${data.base}/current`)).status).toBe(
        "cancelled",
    );
    expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(original);
});
test("fictional closed original period rejects correction and explicit rereview preserves exact input", async ({
    page,
}) => {
    const data = await confirmationSetup(page, "invoice");
    await api(page, "POST", `${data.base}/confirm`, {
        id: randomUUID(),
        expected_version: data.source.version,
    });
    await open(page);
    const reason = "  Fictional rejected original input 中文  ";
    await page.getByLabel("Reason (required)", { exact: true }).fill(reason);
    await page.getByLabel("Unit price before tax", { exact: true }).first().fill("123.40");
    await reviewed(page).check();
    const closed = await api<{ version: number }>(page, "POST", `${data.ledger}/period-changes`, {
        action: "close",
        expected_version: 1,
        closed_through: "2026-01-02",
        reason: "Fictional closed old period",
    });
    const rejected = reply(page, `${data.base}/corrections`);
    await button(page, "Submit whole-document correction").click();
    const r = await rejected;
    expect(r.status()).toBe(409);
    expect((await r.json()).detail.code).toBe("period_closed");
    await expect(
        page.getByText("Original and replacement dates require open periods.", { exact: false }),
    ).toBeVisible();
    await expect(button(page, "Retry original request")).toHaveCount(0);
    await button(page, "Reload and review").click();
    await expect(page.getByLabel("Reason (required)", { exact: true })).toHaveValue(reason);
    await expect(page.getByLabel("Unit price before tax", { exact: true }).first()).toHaveValue(
        "123.40",
    );
    await expect(reviewed(page)).not.toBeChecked();
    await api(page, "POST", `${data.ledger}/period-changes`, {
        action: "reopen",
        expected_version: closed.version,
        closed_through: null,
        reason: "Fictional explicit reopening",
    });
    await reviewed(page).check();
    const accepted = reply(page, `${data.base}/corrections`);
    await button(page, "Submit whole-document correction").click();
    const next = await accepted;
    expect(next.status(), await next.text()).toBe(200);
    expect(next.request().headers()["idempotency-key"]).not.toBe(
        r.request().headers()["idempotency-key"],
    );
    expect(next.request().postData()).toBe(r.request().postData());
    await expect(
        page.getByText("The complete receipt for this revision is verified.", { exact: true }),
    ).toBeVisible();
});
