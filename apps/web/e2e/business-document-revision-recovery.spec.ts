import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, username, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { DocumentCurrent, DocumentRevisionReceipt } from "../src/document-revision-api";
const button = (p: Page, name: string) => p.getByRole("button", { name, exact: true });
const savedMessage = "The complete receipt for this revision is verified.";
const reason = "  Fictional recovery 更正 with exact spaces  ";
async function setup(page: Page, action: "correct" | "cancel" = "correct") {
    const data = await confirmationSetup(page, "bill");
    await api(page, "POST", `${data.base}/confirm`, {
        id: randomUUID(),
        expected_version: data.source.version,
    });
    const original = await api(page, "GET", `${data.base}/confirmation`);
    await button(page, "Confirmed documents").click();
    await button(page, "Current state, correction and cancellation").click();
    await button(
        page,
        action === "correct" ? "Correct whole document" : "Cancel whole document",
    ).click();
    await page.getByLabel("Reason (required)", { exact: true }).fill(reason);
    if (action === "correct")
        await page.getByLabel("Unit price before tax", { exact: true }).first().fill("120.00");
    await page
        .getByRole("checkbox", { name: "I reviewed the current document", exact: false })
        .check();
    return {
        ...data,
        original,
        path: `${data.base}/${action === "correct" ? "corrections" : "cancellations"}`,
        submit:
            action === "correct"
                ? "Submit whole-document correction"
                : "Submit whole-document cancellation",
    };
}
async function signIn(page: Page) {
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await button(page, "Sign in").click();
    await expect(page.getByTestId("current-username")).toHaveText(username);
}
const responseTo = (page: Page, path: string) =>
    page.waitForResponse(
        (r) => new URL(r.url()).pathname === path && r.request().method() === "POST",
    );
async function doubleClick(page: Page, name: string) {
    await button(page, name).evaluate((e) => {
        (e as HTMLButtonElement).click();
        (e as HTMLButtonElement).click();
    });
}
async function unknown(page: Page) {
    await expect(button(page, "Retry original request")).toBeVisible();
    for (const name of [
        "Reload and review",
        "Close",
        "Back to documents",
        "Discard edits and read current version",
    ])
        await expect(button(page, name)).toHaveCount(0);
    await expect(button(page, "Transactions")).toBeDisabled();
    await expect(page.getByText(savedMessage, { exact: true })).toHaveCount(0);
}
for (const action of ["correct", "cancel"] as const)
    test(`fictional whole-document ${action} preserves original receipt across lost200,409,401 and later state`, async ({
        page,
    }, info) => {
        const data = await setup(page, action),
            bodies: string[] = [],
            keys: string[] = [],
            reads: string[] = [];
        let original: DocumentRevisionReceipt | undefined;
        page.on("request", (r) => {
            if (r.method() === "GET" && new URL(r.url()).pathname.startsWith(data.base))
                reads.push(r.url());
        });
        await page.route(`**${data.path}`, async (route) => {
            bodies.push(route.request().postData()!);
            keys.push(route.request().headers()["idempotency-key"]);
            if (bodies.length === 1) {
                const r = await route.fetch();
                expect(r.status(), await r.text()).toBe(200);
                original = await r.json();
                return route.abort("failed");
            }
            if (bodies.length === 2)
                return route.fulfill({
                    status: 409,
                    json: { detail: { code: "version_conflict" } },
                });
            if (bodies.length === 3) {
                const session = await (await page.request.get("/api/v1/auth/session")).json();
                const r = await page.request.post("/api/v1/auth/logout", {
                    headers: {
                        Origin: new URL(page.url()).origin,
                        "X-CSRF-Token": session.csrf_token,
                    },
                });
                expect(r.status()).toBe(204);
            }
            await route.continue();
        });
        await doubleClick(page, data.submit);
        await unknown(page);
        expect(bodies).toHaveLength(1);
        if (!original) throw Error("No real committed receipt");
        expect(JSON.parse(bodies[0]).reason).toBe(reason);
        expect(original.version).toBe(3);
        if (action === "correct") {
            const later = JSON.parse(bodies[0]);
            later.expected_version = 3;
            later.reason = "Fictional later current version";
            later.replacement.lines[0].unit_price = "140.00";
            const advanced = await api<DocumentRevisionReceipt>(page, "POST", data.path, later);
            expect(advanced.version).toBe(4);
        }
        const current = await api<DocumentCurrent>(page, "GET", `${data.base}/current`);
        expect(current.version).toBe(action === "correct" ? 4 : 3);
        expect(await api(page, "GET", `${data.base}/revisions/${original.revision_id}`)).toEqual(
            original,
        );
        expect(reads).toEqual([]);
        await unknown(page);
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width: 320, height: 1000 });
            await button(page, locale === "zh" ? "中文" : "English").click();
            await expect(
                button(page, locale === "zh" ? "重试原请求" : "Retry original request"),
            ).toBeVisible();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`document-${action}-unknown-${locale}-320.png`),
                fullPage: true,
            });
        }
        const conflict = responseTo(page, data.path);
        await button(page, "Retry original request").click();
        expect((await conflict).status()).toBe(409);
        await unknown(page);
        const unauth = responseTo(page, data.path);
        await button(page, "Retry original request").click();
        expect((await unauth).status()).toBe(401);
        await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
        await expect(
            page.getByRole("region", { name: "This document revision", exact: true }),
        ).toHaveCount(0);
        await expect(page.getByText(reason.trim(), { exact: true })).toHaveCount(0);
        await expect(page.getByLabel("Password", { exact: true })).toHaveValue("");
        await signIn(page);
        await unknown(page);
        expect(bodies).toHaveLength(3);
        expect(reads).toEqual([]);
        const replay = responseTo(page, data.path);
        await doubleClick(page, "Retry original request");
        const r = await replay;
        expect(r.status()).toBe(200);
        expect(await r.json()).toEqual(original);
        await expect(page.getByText(savedMessage, { exact: true })).toBeVisible();
        expect(bodies).toHaveLength(4);
        expect(new Set(bodies).size).toBe(1);
        expect(new Set(keys).size).toBe(1);
        expect(reads).toEqual([]);
        expect(await api(page, "GET", `${data.base}/current`)).toEqual(current);
        expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(data.original);
    });
test("fictional partial whole-document receipt stays unknown until the complete original arrives", async ({
    page,
}) => {
    const data = await setup(page),
        bodies: string[] = [],
        keys: string[] = [];
    let original: DocumentRevisionReceipt | undefined;
    await page.route(`**${data.path}`, async (route) => {
        bodies.push(route.request().postData()!);
        keys.push(route.request().headers()["idempotency-key"]);
        if (bodies.length > 1) return route.continue();
        const r = await route.fetch();
        expect(r.status()).toBe(200);
        original = await r.json();
        return route.fulfill({
            response: r,
            json: {
                ...original,
                replacement: {
                    ...original!.replacement,
                    lines: original!.replacement!.lines.slice(0, -1),
                },
            },
        });
    });
    await button(page, data.submit).click();
    await unknown(page);
    const replay = responseTo(page, data.path);
    await button(page, "Retry original request").click();
    expect(await (await replay).json()).toEqual(original);
    await expect(page.getByText(savedMessage, { exact: true })).toBeVisible();
    expect(bodies).toHaveLength(2);
    expect(new Set(bodies).size).toBe(1);
    expect(new Set(keys).size).toBe(1);
    expect(await api(page, "GET", `${data.base}/confirmation`)).toEqual(data.original);
});
test("fictional explicit logout isolates a held real whole-document success after reauthentication", async ({
    page,
}) => {
    const data = await setup(page);
    let release!: () => void, complete!: (r: DocumentRevisionReceipt) => void;
    const gate = new Promise<void>((resolve) => {
            release = resolve;
        }),
        committed = new Promise<DocumentRevisionReceipt>((resolve) => {
            complete = resolve;
        });
    await page.route(`**${data.path}`, async (route) => {
        const r = await route.fetch();
        expect(r.status()).toBe(200);
        complete(await r.json());
        await gate;
        await route.fulfill({ response: r });
    });
    try {
        await button(page, data.submit).click();
        const original = await committed;
        expect(original.version).toBe(3);
        await expect(
            page.getByText("Submitting the original request…", { exact: true }),
        ).toBeVisible();
        await button(page, "Sign out").click();
        await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
        await expect(
            page.getByRole("region", { name: "This document revision", exact: true }),
        ).toHaveCount(0);
        await signIn(page);
        await button(page, "Parties and projects").click();
        const late = responseTo(page, data.path);
        release();
        const r = await late;
        expect(await r.json()).toEqual(original);
        await r.finished();
        await page.evaluate(
            () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
        );
        await expect(
            page.getByRole("heading", { name: "Parties and projects", exact: true }).first(),
        ).toBeVisible();
        await expect(button(page, "Retry original request")).toHaveCount(0);
        await expect(page.getByText(savedMessage, { exact: true })).toHaveCount(0);
        expect(await api(page, "GET", `${data.base}/revisions/${original.revision_id}`)).toEqual(
            original,
        );
    } finally {
        release();
        await page.unroute(`**${data.path}`);
    }
});
