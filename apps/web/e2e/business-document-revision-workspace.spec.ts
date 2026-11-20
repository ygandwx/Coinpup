import { randomUUID } from "node:crypto";
import { expect, test, type Page, type Request, type TestInfo } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessConfirmationReceipt } from "../src/business-confirmation-api";
import type { DocumentCurrent, DocumentRevisionReceipt } from "../src/document-revision-api";
const button = (page: Page, name: string) => page.getByRole("button", { name, exact: true });
const reason = "  Fictional whole document 更正  ";
async function shots(page: Page, info: TestInfo, name: string, editing = false) {
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 1000 });
            await button(page, locale === "zh" ? "中文" : "English").click();
            if (editing) {
                await expect(
                    page.getByLabel(locale === "zh" ? "原因（必填）" : "Reason (required)", {
                        exact: true,
                    }),
                ).toHaveValue(reason);
                await expect(
                    page
                        .getByLabel(locale === "zh" ? "未税单价" : "Unit price before tax", {
                            exact: true,
                        })
                        .first(),
                ).toHaveValue("120.00");
            }
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`document-${name}-${locale}-${width}.png`),
                fullPage: true,
            });
        }
}
async function openCurrent(page: Page) {
    await button(page, "Confirmed documents").click();
    await button(page, "Current state, correction and cancellation").click();
    await expect(
        page.getByRole("region", { name: "Current document facts", exact: true }),
    ).toBeVisible();
}
async function checkMismatchedSnapshot(page: Page, base: string, kind: "invoice" | "bill") {
    const writes: string[] = [];
    const capture = (request: Request) => {
        if (
            new URL(request.url()).pathname.includes("/api/v1/ledgers/") &&
            ["POST", "PUT", "PATCH", "DELETE"].includes(request.method())
        )
            writes.push(request.method());
    };
    page.on("request", capture);
    const marker = "Fictional mismatched snapshot identity";
    let deliveries = 0;
    await page.route(
        `**${base}/current`,
        async (route) => {
            const upstream = await route.fetch();
            expect(upstream.status()).toBe(200);
            const value: DocumentCurrent = await upstream.json();
            const doc = value.document!;
            const snapshot =
                kind === "invoice" ? doc.party_snapshot : doc.lines.at(-1)!.category_snapshot;
            expect(typeof snapshot.id).toBe("string");
            deliveries++;
            if (deliveries === 1) snapshot.id = randomUUID();
            else if (deliveries === 2) {
                if (kind === "invoice") delete snapshot.role;
                else delete snapshot.kind;
            } else if (kind === "invoice") snapshot.role = "supplier";
            else snapshot.version = 0;
            snapshot.name = marker;
            await route.fulfill({ response: upstream, json: value });
        },
        { times: 3 },
    );
    await button(page, "Reload current version").click();
    const alert = page.getByRole("alert");
    await expect(alert.or(page.getByText(marker))).toBeVisible();
    await expect(alert).toBeVisible();
    await expect(page.getByText(marker)).toHaveCount(0);
    await expect(page.getByTestId("document-current-line")).toHaveCount(0);
    await expect(button(page, "Correct whole document")).toHaveCount(0);
    await expect(button(page, "Cancel whole document")).toHaveCount(0);
    await button(page, "中文").click();
    await expect(alert).toBeVisible();
    for (const delivery of [2, 3]) {
        const response = page.waitForResponse(
            (r) => new URL(r.url()).pathname === base + "/current",
        );
        await button(page, "重新读取当前版本").click();
        await response;
        await expect(alert.or(page.getByText(marker))).toBeVisible();
        await expect(alert).toBeVisible();
        await expect(page.getByText(marker)).toHaveCount(0);
        await expect(page.getByTestId("document-current-line")).toHaveCount(0);
        expect(deliveries).toBe(delivery);
    }
    await button(page, "重新读取当前版本").click();
    await expect(page.getByTestId("document-current-line")).toHaveCount(2);
    await expect(alert).toHaveCount(0);
    await button(page, "English").click();
    await expect(button(page, "Correct whole document")).toBeEnabled();
    expect(writes).toEqual([]);
    page.off("request", capture);
}

for (const kind of ["invoice", "bill"] as const)
    test(`fictional ${kind} whole-document workspace correction, immutable original and cancellation`, async ({
        page,
    }, info) => {
        const data = await confirmationSetup(page, kind);
        const confirmation = await api<BusinessConfirmationReceipt>(
            page,
            "POST",
            `${data.base}/confirm`,
            { id: randomUUID(), expected_version: data.source.version },
        );
        const original = await api(page, "GET", `${data.base}/confirmation`);
        await openCurrent(page);
        await expect(page.getByTestId("document-current-line")).toHaveCount(2);
        await checkMismatchedSnapshot(page, data.base, kind);
        await button(page, "Correct whole document").click();
        const form = page.getByRole("form", { name: "Correct whole document", exact: true });
        await form.getByLabel("Reason (required)", { exact: true }).fill(reason);
        await form.getByLabel("Unit price before tax", { exact: true }).first().fill("120.00");
        await expect(button(page, "Submit whole-document correction")).toBeDisabled();
        const reviewed = form.getByRole("checkbox", {
            name: "I reviewed the current document",
            exact: false,
        });
        await reviewed.check();
        await shots(page, info, `${kind}-editor`, true);
        await expect(reviewed).toBeChecked();
        // An exact-input edit invalidates human review, even with unchanged numeric value.
        await form.getByLabel("Unit price before tax", { exact: true }).first().fill("120.0");
        await expect(reviewed).not.toBeChecked();
        await form.getByLabel("Unit price before tax", { exact: true }).first().fill("120.00");
        await reviewed.check();
        const next = page.waitForResponse(
            (r) =>
                new URL(r.url()).pathname === `${data.base}/corrections` &&
                r.request().method() === "POST",
        );
        await button(page, "Submit whole-document correction").press("Enter");
        const response = await next;
        expect(response.status(), await response.text()).toBe(200);
        const receipt = (await response.json()) as DocumentRevisionReceipt;
        expect(receipt.previous.lines).toHaveLength(2);
        expect(receipt.replacement!.lines).toHaveLength(2);
        expect(receipt.replacement!.lines[0].unit_price).toBe("120.00");
        expect(receipt.replacement!.lines[1].posting).toBeNull();
        expect(receipt.reversals).toHaveLength(1);
        await expect(
            page.getByText("The complete receipt for this revision is verified.", { exact: true }),
        ).toBeVisible();
        await expect(page.getByTestId("document-version-line")).toHaveCount(4);
        await shots(page, info, `${kind}-receipt`);
        expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(original);
        const current = await api<DocumentCurrent>(page, "GET", `${data.base}/current`);
        expect(current.version).toBe(confirmation.document_version + 1);
        expect(current.document!.lines[0].total_amount).toBe("132.00");
        await button(page, "Close").click();
        await openCurrent(page);
        await button(page, "Cancel whole document").click();
        const cancellation = page.getByRole("form", { name: "Cancel whole document", exact: true });
        await cancellation
            .getByLabel("Reason (required)", { exact: true })
            .fill("Fictional explicit cancellation 中文");
        await cancellation
            .getByRole("checkbox", { name: "I reviewed the current document", exact: false })
            .check();
        const cancelled = page.waitForResponse(
            (r) =>
                new URL(r.url()).pathname === `${data.base}/cancellations` &&
                r.request().method() === "POST",
        );
        await button(page, "Submit whole-document cancellation").click();
        const cancelResponse = await cancelled;
        expect(cancelResponse.status(), await cancelResponse.text()).toBe(200);
        const terminal = (await cancelResponse.json()) as DocumentRevisionReceipt;
        expect(terminal.replacement).toBeNull();
        await expect(
            page.getByText("The complete receipt for this revision is verified.", { exact: true }),
        ).toBeVisible();
        expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(original);
        await button(page, "Close").click();
        await button(page, "Current state, correction and cancellation").click();
        await expect(
            page.getByText("This document is cancelled and has no current accruals.", {
                exact: true,
            }),
        ).toBeVisible();
        await expect(button(page, "Correct whole document")).toHaveCount(0);
        await expect(button(page, "Cancel whole document")).toHaveCount(0);
    });
