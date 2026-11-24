import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { invoiceSetup } from "./invoice-helpers";
import type { InvoiceComplete, InvoicePage } from "../src/invoice-issuance-receipt";
test("review, uncertain submission and reissue bind two immutable numbers and both original PDF pairs", async ({
    page,
}, info) => {
    const { company, ledger, doc, confirmation, profile, category, customer } =
        await invoiceSetup(page);
    const url = ledger + "/invoice-issuances";
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    async function choose() {
        await button("Issue commercial invoice").click();
        await page
            .getByRole("combobox", { name: "Confirmed invoice", exact: true })
            .selectOption(doc.id);
        await page
            .getByRole("combobox", { name: "Issuance profile", exact: true })
            .selectOption(profile.id);
        await expect(button("Confirm issuance")).toBeDisabled();
        await page.getByRole("checkbox", { name: "I reviewed the document", exact: false }).check();
    }
    await page.goto(`/?entity=${company.id}&view=invoices`);
    await choose();
    await expect(page.getByRole("region", { name: "Current document facts" })).toContainText(
        "54.13",
    );
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 950 });
            await button(locale === "zh" ? "中文" : "English").click();
            await expect(page.getByRole("checkbox").last()).toBeChecked();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`invoice-issue-review-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    const result = page.getByRole("region", { name: /^(Issuance result|签发结果)$/u });
    async function logoScreens(code: string, locale: string) {
        for (const width of [1440, 375, 320]) {
            await page.setViewportSize({ width, height: 950 });
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`invoice-issue-logo-${code}-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    }
    let logoCode = "invoice_logo_size_limit";
    await page.route("**" + url, (route) =>
        route.request().method() === "POST"
            ? route.fulfill({ status: 422, json: { detail: { code: logoCode } } })
            : route.continue(),
    );
    for (const [code, english, chinese] of [
        ["invoice_logo_size_limit", "8 MiB or less", "Logo 不超过 8 MiB"],
        ["invoice_logo_invalid", "single-frame PNG, JPEG or WebP", "单帧 PNG、JPEG 或 WebP"],
    ]) {
        logoCode = code;
        await button("Confirm issuance").click();
        await expect(result.getByRole("alert")).toContainText(english);
        await expect(button("Retry original issuance")).toHaveCount(0);
        await logoScreens(code, "en");
        await button("中文").click();
        await expect(result.getByRole("alert")).toContainText(chinese);
        await logoScreens(code, "zh");
        await button("English").click();
        await button("Close").click();
        await choose();
    }
    await page.unroute("**" + url);
    expect((await api<InvoicePage>(page, "GET", url)).items).toEqual([]);

    let first: InvoiceComplete | null = null;
    const writes: { key: string | undefined; body: string | null }[] = [];
    await page.route("**" + url, async (route) => {
        if (route.request().method() !== "POST") {
            await route.continue();
            return;
        }
        writes.push({
            key: route.request().headers()["idempotency-key"],
            body: route.request().postData(),
        });
        if (writes.length === 2)
            return route.fulfill({
                status: 422,
                json: { detail: { code: "invoice_render_logo_limit" } },
            });
        const res = await route.fetch();
        expect(res.status(), await res.text()).toBe(201);
        if (writes.length === 1) {
            first = await res.json();
            await route.abort("failed");
        } else await route.fulfill({ response: res });
    });
    await button("Confirm issuance").press("Enter");
    await expect(button("Retry original issuance")).toBeVisible();
    await expect(button("Credits")).toBeDisabled();
    await button("Inspect original intent").click();
    await expect(
        page.getByText(/Originals sealed; this request is still unconfirmed/u),
    ).toBeVisible();
    await button("Retry original issuance").click();
    await expect(result.getByRole("alert")).toContainText("reserved number");
    await expect(button("Close")).toHaveCount(0);
    await expect(button("Inspect original intent")).toBeVisible();
    await logoScreens("reserved", "en");
    await button("中文").click();
    await expect(result.getByRole("alert")).toContainText("保留原请求");
    await logoScreens("reserved", "zh");
    await button("English").click();
    await button("Retry original issuance").click();
    await expect(
        page.getByText("Both originals for this issuance are verified.", { exact: true }),
    ).toBeVisible();
    expect(writes.length).toBe(3);
    expect(writes[0].key).toBeTruthy();
    expect(writes.slice(1)).toEqual([writes[0], writes[0]]);
    const original = first as InvoiceComplete | null;
    if (!original) throw new Error("Missing first real issuance");
    expect(original.number).toBe("示例-0001");
    const fileHash = async (file: string) => {
        const res = await page.request.get(ledger + "/files/" + file + "/content");
        expect(res.status()).toBe(200);
        return createHash("sha256")
            .update(await res.body())
            .digest("hex");
    };
    for (const f of original.artifacts) expect(await fileHash(f.file_id)).toBe(f.sha256);
    await button("Close").click();
    await api(page, "POST", ledger + "/business-documents/" + doc.id + "/corrections", {
        expected_version: confirmation.document_version,
        reason: "Fictional explicit change",
        replacement: {
            document_kind: "invoice",
            party_id: customer.id,
            asset_id: "USD",
            issue_date: "2026-11-14",
            due_date: "2026-12-14",
            lines: [
                {
                    id: doc.lines[0].id,
                    description: "Fictional corrected 中文",
                    quantity: "3",
                    unit_price: "20.00",
                    discount_amount: "9.97",
                    tax_rate_percent: "8.25",
                    category_id: category.id,
                    recognition_date: "2026-11-14",
                },
            ],
            reviewed_references: [],
        },
    });
    await choose();
    await expect(page.getByText("示例-0001", { exact: true })).toBeVisible();
    await button("Confirm issuance").click();
    await expect(page.getByText("Issued number: 示例-0002")).toBeVisible();
    expect(writes.length).toBe(4);
    expect(writes[3].key).not.toBe(writes[0].key);
    expect(JSON.parse(writes[3].body!).previous_issuance_id).toBe(original.id);
    await button("Close").click();
    await button("示例-0001").click();
    await expect(page.getByText(/Document corrected · Issued version/u)).toBeVisible();
    const pageData = await api<InvoicePage>(page, "GET", url);
    expect(pageData.items.map((v) => [v.number, v.document_status])).toEqual([
        ["示例-0002", "current"],
        ["示例-0001", "superseded"],
    ]);
    for (const f of original.artifacts) expect(await fileHash(f.file_id)).toBe(f.sha256);
    const newer = await api<InvoiceComplete>(page, "GET", url + "/" + pageData.items[0].id);
    for (const f of newer.artifacts) {
        expect(await fileHash(f.file_id)).toBe(f.sha256);
        expect(original.artifacts.map((a) => a.file_id)).not.toContain(f.file_id);
    }
});
