import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { DocumentRevisionReceipt } from "../src/document-revision-api";
const button = (p: Page, name: string) => p.getByRole("button", { name, exact: true });
async function setup(page: Page) {
    const d = await confirmationSetup(page, "bill");
    await api(page, "POST", `${d.base}/confirm`, {
        id: randomUUID(),
        expected_version: d.source.version,
    });
    const replacement = {
        document_kind: "bill",
        party_id: d.party.id,
        asset_id: "USD",
        issue_date: "2026-01-02",
        due_date: null,
        notes: "Fictional history guard notes",
        reviewed_references: [],
        lines: d.source.lines.map((r) => ({
            id: r.id,
            description: r.description,
            quantity: r.quantity,
            unit_price: r.unit_price,
            discount_amount: r.discount_amount,
            tax_rate_percent: r.tax_rate_percent,
            category_id: r.category_id,
            project_id: r.project_id,
            recognition_date: r.recognition_date,
        })),
    };
    const first = await api<DocumentRevisionReceipt>(page, "POST", `${d.base}/corrections`, {
        expected_version: 2,
        reason: "Fictional first fixed reason",
        replacement,
    });
    const second = await api<DocumentRevisionReceipt>(page, "POST", `${d.base}/corrections`, {
        expected_version: 3,
        reason: "Fictional second fixed reason",
        replacement,
    });
    await button(page, "Confirmed documents").click();
    await button(page, "Current state, correction and cancellation").click();
    await button(page, "View version history").click();
    const region = page.getByRole("region", { name: "Document version history", exact: true });
    await expect(region.locator(".master-records > li")).toHaveCount(3);
    return { ...d, first, second, region };
}
async function select(page: Page, version: number) {
    await page
        .getByRole("region", { name: "Document version history", exact: true })
        .locator(".master-records > li")
        .filter({ has: page.getByText(new RegExp(`^Version ${version} ·`)) })
        .getByRole("button", { name: "View fixed details", exact: true })
        .click();
}
test("fictional foreign or partial fixed-history delivery fails closed until a complete reread", async ({
    page,
}) => {
    const d = await setup(page),
        path = `${d.base}/revisions/${d.first.revision_id}`;
    let count = 0;
    await page.route(`**${path}`, async (route) => {
        count++;
        const r = await route.fetch();
        expect(r.status()).toBe(200);
        const value = await r.json();
        if (count === 1) value.ledger_id = randomUUID();
        if (count === 2) value.replacement.lines.pop();
        await route.fulfill({ response: r, json: value });
    });
    await select(page, 3);
    for (let i = 0; i < 2; i++) {
        await expect(
            page.getByText("The complete history could not be verified. Reload to try again.", {
                exact: true,
            }),
        ).toBeVisible();
        await expect(page.getByTestId("document-version-line")).toHaveCount(0);
        await expect(page.getByText("Fictional history guard notes", { exact: true })).toHaveCount(
            0,
        );
        await button(page, "Reload history").click();
    }
    await expect(page.getByTestId("document-version-line")).toHaveCount(4);
    expect(count).toBe(3);
    await expect(d.region).toContainText(d.first.revision_id);
});
test("fictional held old history cannot replace a later selected fixed revision", async ({
    page,
}) => {
    const d = await setup(page),
        path = `${d.base}/revisions/${d.first.revision_id}`;
    let release!: () => void, entered!: () => void;
    const gate = new Promise<void>((r) => {
            release = r;
        }),
        held = new Promise<void>((r) => {
            entered = r;
        });
    await page.route(`**${path}`, async (route) => {
        const response = await route.fetch();
        entered();
        await gate;
        try {
            await route.fulfill({ response });
        } catch {
            /* Selection abort may close transport; the visible state remains the new selection. */
        }
    });
    try {
        await select(page, 3);
        await held;
        await button(page, "Back to history list").click();
        await select(page, 4);
        await expect(d.region).toContainText(d.second.revision_id);
        release();
        await page.evaluate(
            () =>
                new Promise<void>((r) =>
                    requestAnimationFrame(() => requestAnimationFrame(() => r())),
                ),
        );
        await expect(d.region).toContainText(d.second.revision_id);
        await expect(d.region).not.toContainText(d.first.revision_id);
        await expect(page.getByTestId("document-version-line")).toHaveCount(4);
    } finally {
        release();
        await page.unroute(`**${path}`);
    }
});
test("fictional real401 during history detail hides all private document facts", async ({
    page,
}) => {
    const d = await setup(page),
        path = `${d.base}/revisions/${d.first.revision_id}`;
    await page.route(`**${path}`, async (route) => {
        const session = await (await page.request.get("/api/v1/auth/session")).json();
        const response = await page.request.post("/api/v1/auth/logout", {
            headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": session.csrf_token },
        });
        expect(response.status()).toBe(204);
        await route.continue();
    });
    await select(page, 3);
    await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
    await expect(d.region).toHaveCount(0);
    await expect(page.getByText("Fictional first fixed reason", { exact: true })).toHaveCount(0);
    await expect(page.getByTestId("document-current-line")).toHaveCount(0);
    await expect(page.getByTestId("document-version-line")).toHaveCount(0);
});
