import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessDraft } from "../src/business-draft-api";
import type { DocumentRevisionReceipt } from "../src/document-revision-api";
const button = (page: Page, name: string) => page.getByRole("button", { name, exact: true });
async function setup(page: Page, big = false) {
    const data = await confirmationSetup(page, "invoice");
    const replacement = {
        document_kind: "invoice" as const,
        party_id: data.party.id,
        asset_id: "USD",
        issue_date: "2026-01-02",
        due_date: "2026-02-01",
        notes: "Fictional fixed history 中文",
        lines: Array.from({ length: big ? 200 : 2 }, (_, i) => ({
            id: randomUUID(),
            description: `Fictional source ${i} 中文`,
            quantity: "1.00",
            unit_price: i % 3 === 0 ? "0" : "100.00",
            discount_amount: "0",
            tax_rate_percent: "10",
            category_id: data.source.lines[0].category_id,
            recognition_date: "2026-01-02",
        })),
    };
    let source = await api<BusinessDraft>(page, "PUT", data.base, {
        ...replacement,
        expected_version: data.source.version,
    });
    source = await api<BusinessDraft>(page, "PUT", data.base, {
        ...replacement,
        expected_version: source.version,
    });
    await api(page, "POST", `${data.base}/confirm`, {
        id: randomUUID(),
        expected_version: source.version,
    });
    const original = await api(page, "GET", `${data.base}/confirmation`);
    return { ...data, replacement, original, version: source.version + 1 };
}
async function history(page: Page, entityId: string) {
    await page.goto(`/?entity=${entityId}&view=business-confirmations`);
    await button(page, "Confirmed documents").click();
    await button(page, "Current state, correction and cancellation").click();
    await button(page, "View version history").click();
    await expect(
        page.getByRole("heading", { name: "Document version history", exact: true }),
    ).toBeVisible();
}
async function detail(page: Page, version: number) {
    const region = page.getByRole("region", { name: "Document version history", exact: true });
    await region
        .locator(".master-records > li")
        .filter({ has: page.getByText(new RegExp(`^Version ${version} ·`)) })
        .getByRole("button", { name: "View fixed details", exact: true })
        .click();
    return region;
}
test("fictional history pages from actual original version4 and keeps old revisions after later cancellation", async ({
    page,
}, info) => {
    const data = await setup(page);
    expect(data.version).toBe(4);
    let first: DocumentRevisionReceipt | undefined;
    for (let i = 0; i < 25; i++) {
        const r = await api<DocumentRevisionReceipt>(page, "POST", `${data.base}/corrections`, {
            expected_version: data.version + i,
            reason: `Fictional fixed correction ${i}`,
            replacement: {
                ...data.replacement,
                notes: `Fictional version ${5 + i}`,
                reviewed_references: [],
            },
        });
        if (i === 0) first = r;
    }
    const cancelled = await api<DocumentRevisionReceipt>(
        page,
        "POST",
        `${data.base}/cancellations`,
        { expected_version: 29, reason: "Fictional terminal cancellation" },
    );
    expect(cancelled.version).toBe(30);
    await history(page, data.entity.id);
    const region = page.getByRole("region", { name: "Document version history", exact: true });
    await expect(region.locator(".master-records > li")).toHaveCount(25);
    await detail(page, 4);
    await expect(
        page.getByRole("region", { name: "Original document facts", exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("document-current-line")).toHaveCount(2);
    await button(page, "Back to history list").click();
    await detail(page, 5);
    await expect(region).toContainText(first!.revision_id);
    await expect(page.getByTestId("document-version-line")).toHaveCount(4);
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 1000 });
            await button(page, locale === "zh" ? "中文" : "English").click();
            await expect(page.getByTestId("document-version-line")).toHaveCount(4);
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`document-history-fixed-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    await button(page, "Back to history list").click();
    await button(page, "Next page").click();
    await expect(region.locator(".master-records > li")).toHaveCount(2);
    await expect(button(page, "Next page")).toBeDisabled();
    await detail(page, 30);
    await expect(region).toContainText(cancelled.revision_id);
    await expect(
        page.getByText("This cancellation has no replacement document.", { exact: true }),
    ).toBeVisible();
    await button(page, "Back to history list").click();
    await button(page, "Previous page").click();
    await detail(page, 5);
    await expect(region).toContainText(first!.revision_id);
    expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(data.original);
});
test("fictional fixed200member history returns complete400facts and all133reversals in one detail read", async ({
    page,
}, info) => {
    test.setTimeout(90_000);
    const data = await setup(page, true);
    const corrected = await api<DocumentRevisionReceipt>(page, "POST", `${data.base}/corrections`, {
        expected_version: data.version,
        reason: "Fictional all200sources",
        replacement: {
            ...data.replacement,
            lines: data.replacement.lines.map((r) => ({ ...r, unit_price: "120.00" })),
            reviewed_references: [],
        },
    });
    expect(corrected.previous.lines).toHaveLength(200);
    expect(corrected.replacement!.lines).toHaveLength(200);
    expect(corrected.reversals).toHaveLength(133);
    await api(page, "POST", `${data.base}/cancellations`, {
        expected_version: 5,
        reason: "Fictional cancel all200",
    });
    await history(page, data.entity.id);
    const requests: string[] = [];
    page.on("request", (r) => {
        if (r.method() === "GET" && new URL(r.url()).pathname.startsWith(`${data.base}/revisions/`))
            requests.push(r.url());
    });
    await detail(page, 5);
    await expect(page.getByTestId("document-version-line")).toHaveCount(400);
    await expect(
        page.getByRole("region", { name: "Complete reversal journals", exact: true }).locator("li"),
    ).toHaveCount(133);
    for (const locale of ["zh", "en"]) {
        await page.setViewportSize({ width: 320, height: 1000 });
        await button(page, locale === "zh" ? "中文" : "English").click();
        await page.getByTestId("document-version-line").last().scrollIntoViewIfNeeded();
        await expectNoOverflow(page);
        await expect(page.getByTestId("document-version-line").last()).toContainText(
            data.replacement.lines[199].id,
        );
        await page.screenshot({
            path: info.outputPath(`document-history200-last-${locale}-320.png`),
        });
    }
    expect(requests).toHaveLength(1);
    expect(new URL(requests[0]).pathname).toBe(`${data.base}/revisions/${corrected.revision_id}`);
    expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(data.original);
});
