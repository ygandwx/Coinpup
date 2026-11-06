import { expect, test, type Page } from "@playwright/test";
import { api, username } from "./helpers";
import {
    button,
    editReplacement,
    fixedPost,
    openCurrent,
    reviewed,
    revisionReason,
    revisionSetup,
    shots,
} from "./credit-revision-flow-helpers";
import type { CreditCurrent, CreditHistoryEntry, CreditRevisionReceipt } from "../src/credit-api";

const savedMessage =
    "This revision is saved and its complete transition receipt is verified. This receipt does not represent today's current state.";
const cancellationReason = "  Fictional cancellation recovery 中文 🧾  ";
async function signInSamePage(page: Page) {
    // Do not navigate/reload: the original controller exists only in this page's memory.
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await button(page, "Sign in").click();
    await expect(page.getByTestId("current-username")).toHaveText(username);
}
async function unknown(page: Page) {
    await expect(button(page, "Retry original request")).toBeVisible();
    await expect(
        page.getByRole("region", { name: "This credit revision", exact: true }),
    ).toContainText("The outcome is unknown.");
    for (const label of [
        "Edit rejected request",
        "Close this revision receipt",
        "Discard edits and return to current state",
        "View version history",
    ])
        await expect(button(page, label)).toHaveCount(0);
    await expect(button(page, "Transactions")).toBeDisabled();
    await expect(page.getByText(savedMessage, { exact: true })).toHaveCount(0);
}
const responseTo = (page: Page, path: string) =>
    page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === path && response.request().method() === "POST",
    );
async function doubleClick(page: Page, label: string) {
    await button(page, label).evaluate((element) => {
        (element as HTMLButtonElement).click();
        (element as HTMLButtonElement).click();
    });
}

for (const action of ["correct", "cancel"] as const)
    test(`fictional ${action} keeps the original intent through a lost real 200, injected 409 and real 401`, async ({
        page,
    }, info) => {
        const data = await revisionSetup(page, "bill");
        await openCurrent(page, data.entity.id);
        if (action === "correct") await editReplacement(page, ["A", "C"]);
        else {
            await button(page, "Cancel complete credit group").click();
            await page
                .getByLabel("Reason for this correction or cancellation", { exact: true })
                .fill(cancellationReason);
            await reviewed(page).check();
        }
        const path = `${data.endpoint}/${action === "correct" ? "corrections" : "cancellations"}`;
        const bodies: string[] = [],
            keys: string[] = [],
            readRequests: string[] = [];
        let committed: CreditRevisionReceipt | undefined;
        page.on("request", (request) => {
            const pathname = new URL(request.url()).pathname;
            if (
                request.method() === "GET" &&
                (pathname === data.endpoint ||
                    pathname.startsWith(`${data.endpoint}/`) ||
                    pathname === data.base ||
                    pathname.startsWith(`${data.base}/`))
            )
                readRequests.push(pathname);
        });
        await page.route(`**${path}`, async (route) => {
            if (route.request().method() !== "POST") return route.continue();
            bodies.push(route.request().postData()!);
            keys.push(route.request().headers()["idempotency-key"]);
            if (bodies.length === 1) {
                const response = await route.fetch();
                expect(response.status(), await response.text()).toBe(200);
                committed = await response.json();
                return route.abort("failed");
            }
            if (bodies.length === 2) {
                // Inject an intermediate rejection after uncertainty. A real same-key server replay is 200.
                return route.fulfill({
                    status: 409,
                    json: { detail: { code: "version_conflict" } },
                });
            }
            if (bodies.length === 3) {
                const session = await page.request.get("/api/v1/auth/session");
                expect(session.status()).toBe(200);
                const loggedOut = await page.request.post("/api/v1/auth/logout", {
                    headers: {
                        Origin: new URL(page.url()).origin,
                        "X-CSRF-Token": (await session.json()).csrf_token,
                    },
                });
                expect(loggedOut.status()).toBe(204);
            }
            await route.continue();
        });
        await doubleClick(
            page,
            action === "correct" ? "Confirm correction" : "Confirm credit cancellation",
        );
        await unknown(page);
        expect(bodies).toHaveLength(1);
        if (!committed)
            throw new Error("The fictional revision did not reach its real 200 barrier");
        const original = committed;
        expect(JSON.parse(bodies[0])).toMatchObject({
            expected_version: 1,
            reason: action === "correct" ? revisionReason : cancellationReason,
        });
        expect(original).toMatchObject({ version: 2, previous_version: 1, action });
        let later: CreditRevisionReceipt | null = null;
        if (action === "correct") {
            // Another legitimate intent advances current state; recovery must still return our fixed v2.
            later = await api<CreditRevisionReceipt>(page, "POST", path, {
                expected_version: 2,
                reason: "Fictional later correction while the original result is unknown",
                replacement: {
                    credit_date: "2026-01-07",
                    reason: "Fictional current v3 facts",
                    lines: original.members
                        .filter((member) => member.line_no !== null)
                        .map((member) => ({
                            source_line_id: member.source_line_id,
                            net_amount: "21.00",
                            tax_amount: "3.00",
                            recognition_date: "2026-01-07",
                        })),
                },
            });
            expect(later).toMatchObject({ version: 3, previous_version: 2, action: "correct" });
            expect(later.revision_id).not.toBe(original.revision_id);
            expect(bodies).toHaveLength(1);
        }
        // API-context reads prove actual commit, but never deliver success evidence to the page controller.
        const current = await api<CreditCurrent>(page, "GET", `${data.endpoint}/current`);
        expect(current).toMatchObject({
            version: action === "correct" ? 3 : 2,
            status: action === "correct" ? "active" : "cancelled",
            revision_id: later?.revision_id ?? original.revision_id,
        });
        if (action === "cancel") expect(current.members).toEqual([]);
        const history = await api<CreditHistoryEntry[]>(
            page,
            "GET",
            `${data.endpoint}/history?limit=25&offset=0`,
        );
        expect(history.map((row) => row.version)).toEqual(
            action === "correct" ? [1, 2, 3] : [1, 2],
        );
        expect(
            await api(page, "GET", `${data.endpoint}/revisions/${original.revision_id}`),
        ).toEqual(original);
        await unknown(page);
        expect(readRequests).toEqual([]);
        await shots(
            page,
            info,
            `${action}-unknown-recovery`,
            [
                ["zh", 320],
                ["en", 375],
            ],
            async (locale) => {
                await expect(
                    button(page, locale === "zh" ? "重试原请求" : "Retry original request"),
                ).toBeVisible();
                await expect(
                    page.getByRole("region", {
                        name: locale === "zh" ? "本次贷项修订" : "This credit revision",
                        exact: true,
                    }),
                ).toContainText(
                    (action === "correct" ? revisionReason : cancellationReason).trim(),
                );
            },
        );
        const injected = responseTo(page, path);
        await button(page, "Retry original request").click();
        expect((await injected).status()).toBe(409);
        await unknown(page);
        expect(bodies).toHaveLength(2);
        const unauthorized = responseTo(page, path);
        await button(page, "Retry original request").click();
        expect((await unauthorized).status()).toBe(401);
        await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
        await expect(
            page.getByRole("region", { name: "This credit revision", exact: true }),
        ).toHaveCount(0);
        await expect(
            page.getByText((action === "correct" ? revisionReason : cancellationReason).trim(), {
                exact: true,
            }),
        ).toHaveCount(0);
        await expect(page.getByLabel("Password", { exact: true })).toHaveValue("");
        await shots(page, info, `${action}-auth-required`, [["en", 375]]);
        await signInSamePage(page);
        await unknown(page);
        expect(bodies).toHaveLength(3); // Re-authentication must not retry automatically.
        expect(readRequests).toEqual([]);
        const replay = responseTo(page, path);
        await doubleClick(page, "Retry original request");
        const saved = await replay;
        expect(saved.status()).toBe(200);
        expect(await saved.json()).toEqual(original);
        await expect(page.getByText(savedMessage, { exact: true })).toBeVisible();
        expect(bodies).toHaveLength(4);
        expect(new Set(bodies).size).toBe(1);
        expect(new Set(keys).size).toBe(1);
        expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/u);
        expect(readRequests).toEqual([]);
        expect(await api(page, "GET", `${data.endpoint}/current`)).toEqual(current);
        expect(await api(page, "GET", `${data.endpoint}/history?limit=25&offset=0`)).toEqual(
            history,
        );
        expect(await api(page, "GET", data.endpoint)).toEqual(data.detail);
        expect(await fixedPost(page, data.path, data.raw, data.key, 201)).toEqual(data.original);
        expect(await api(page, "GET", `${data.ledger}/balances?limit=100`)).toEqual(data.balances);
    });

test("fictional partial real 200 remains unknown until the complete original group receipt arrives", async ({
    page,
}, info) => {
    const data = await revisionSetup(page, "invoice");
    await openCurrent(page, data.entity.id);
    await editReplacement(page, ["A", "C"]);
    const path = `${data.endpoint}/corrections`,
        bodies: string[] = [],
        keys: string[] = [];
    let committed: CreditRevisionReceipt | undefined;
    await page.route(`**${path}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        bodies.push(route.request().postData()!);
        keys.push(route.request().headers()["idempotency-key"]);
        if (bodies.length !== 1) return route.continue();
        const response = await route.fetch();
        expect(response.status(), await response.text()).toBe(200);
        committed = await response.json();
        expect(committed!.members).toHaveLength(3);
        // Remove the reversed B member from delivery only; retain the real complete response for comparison.
        await route.fulfill({
            response,
            json: { ...committed, members: committed!.members.slice(0, -1) },
        });
    });
    await button(page, "Confirm correction").click();
    await unknown(page);
    expect(bodies).toHaveLength(1);
    if (!committed) throw new Error("The fictional partial response has no actual saved receipt");
    const original = committed;
    expect((await api<CreditCurrent>(page, "GET", `${data.endpoint}/current`)).version).toBe(2);
    await shots(page, info, "partial-receipt-unknown", [["en", 320]]);
    const replay = responseTo(page, path);
    await button(page, "Retry original request").click();
    const saved = await replay;
    expect(saved.status()).toBe(200);
    expect(await saved.json()).toEqual(original);
    await expect(page.getByText(savedMessage, { exact: true })).toBeVisible();
    expect(bodies).toHaveLength(2);
    expect(new Set(bodies).size).toBe(1);
    expect(new Set(keys).size).toBe(1);
    expect(await api(page, "GET", `${data.endpoint}/revisions/${original.revision_id}`)).toEqual(
        original,
    );
    expect(
        (
            await api<CreditHistoryEntry[]>(
                page,
                "GET",
                `${data.endpoint}/history?limit=25&offset=0`,
            )
        ).map((row) => row.version),
    ).toEqual([1, 2]);
    expect(await api(page, "GET", data.endpoint)).toEqual(data.detail);
    expect(await api(page, "GET", `${data.ledger}/balances?limit=100`)).toEqual(data.balances);
});

test("fictional explicit logout isolates a held real credit revision success after same-page login", async ({
    page,
}) => {
    const data = await revisionSetup(page, "bill");
    await openCurrent(page, data.entity.id);
    await editReplacement(page, ["A", "C"]);
    const path = `${data.endpoint}/corrections`;
    let release!: () => void,
        completed!: (value: { status: number; receipt: CreditRevisionReceipt }) => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    const committed = new Promise<{ status: number; receipt: CreditRevisionReceipt }>((resolve) => {
        completed = resolve;
    });
    let raw = "",
        key = "";
    await page.route(`**${path}`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        raw = route.request().postData()!;
        key = route.request().headers()["idempotency-key"];
        const response = await route.fetch();
        completed({ status: response.status(), receipt: await response.json() });
        await gate;
        await route.fulfill({ response });
    });
    try {
        await button(page, "Confirm correction").click();
        const actual = await committed;
        expect(actual.status).toBe(200);
        const original = actual.receipt;
        expect(original.version).toBe(2);
        await expect(
            page.getByText("Submitting the frozen original intent…", { exact: true }),
        ).toBeVisible();
        await button(page, "Sign out").click();
        await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
        await expect(
            page.getByRole("region", { name: "This credit revision", exact: true }),
        ).toHaveCount(0);
        await expect(page.getByText(revisionReason.trim(), { exact: true })).toHaveCount(0);
        await signInSamePage(page);
        await button(page, "Parties and projects").click();
        const delivered = responseTo(page, path);
        release();
        const late = await delivered;
        expect(late.status()).toBe(200);
        expect(await late.json()).toEqual(original);
        await late.finished();
        await page.evaluate(
            () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
        );
        await expect(
            page.getByRole("heading", { name: "Parties and projects", exact: true }).first(),
        ).toBeVisible();
        await expect(
            page.getByRole("region", { name: "This credit revision", exact: true }),
        ).toHaveCount(0);
        await expect(button(page, "Retry original request")).toHaveCount(0);
        await expect(page.getByText(savedMessage, { exact: true })).toHaveCount(0);
        expect(await fixedPost(page, path, raw, key, 200)).toEqual(original);
        expect(
            await api(page, "GET", `${data.endpoint}/revisions/${original.revision_id}`),
        ).toEqual(original);
        expect(
            (
                await api<CreditHistoryEntry[]>(
                    page,
                    "GET",
                    `${data.endpoint}/history?limit=25&offset=0`,
                )
            ).map((row) => row.version),
        ).toEqual([1, 2]);
        expect(await api(page, "GET", data.endpoint)).toEqual(data.detail);
        expect(await api(page, "GET", `${data.ledger}/balances?limit=100`)).toEqual(data.balances);
    } finally {
        release();
        await page.unroute(`**${path}`);
    }
});
