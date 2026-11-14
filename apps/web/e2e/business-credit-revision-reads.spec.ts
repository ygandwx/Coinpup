import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { api, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import { button, openCurrent, reviewed, shots } from "./credit-revision-flow-helpers";
import type { BusinessConfirmationReceipt } from "../src/business-confirmation-api";
import type { CreditCurrent, CreditDetail, CreditRevisionReceipt } from "../src/credit-api";

const narrow = [
    ["zh", 320],
    ["en", 375],
] as const;
const history = (page: Page, locale = "en") =>
    page.getByRole("region", {
        name: locale === "zh" ? "贷项版本历史" : "Credit version history",
        exact: true,
    });
const reasonLabel = "Reason for this correction or cancellation";
const retainedReason = "  Fictional retained editor input 中文  ";

async function setup(page: Page) {
    const data = await confirmationSetup(page, "bill");
    const confirmed = await api<BusinessConfirmationReceipt>(page, "POST", `${data.base}/confirm`, {
        id: randomUUID(),
        expected_version: data.source.version,
    });
    const replacement = {
        credit_date: "2026-01-05",
        reason: "Fictional reader original facts",
        lines: [
            {
                source_line_id: data.source.lines[0].id,
                net_amount: "17.23",
                tax_amount: "2.77",
                recognition_date: "2026-01-05",
            },
        ],
    };
    const creditId = randomUUID(),
        endpoint = `${data.ledger}/business-credits/${creditId}`;
    await api(page, "POST", `${data.ledger}/business-credits`, {
        id: creditId,
        document_id: data.source.id,
        expected_version: confirmed.document_version,
        ...replacement,
    });
    const original = await api<CreditDetail>(page, "GET", endpoint);
    return { ...data, endpoint, replacement, original };
}

async function openEditor(page: Page) {
    await button(page, "Correct complete credit group").click();
    const form = page.getByRole("form", { name: "Correct credit group", exact: true });
    await expect(form).toBeVisible();
    await form.getByLabel(reasonLabel, { exact: true }).fill(retainedReason);
    return form;
}

test("fictional directory and fixed-detail failures stay failed until explicit reload", async ({
    page,
}, info) => {
    const data = await setup(page);
    const revision = await api<CreditRevisionReceipt>(
        page,
        "POST",
        `${data.endpoint}/corrections`,
        {
            expected_version: 1,
            reason: "Fictional fixed reader revision",
            replacement: data.replacement,
        },
    );
    await openCurrent(page, data.entity.id);
    let directoryFail = true,
        detailFail = true,
        directoryReads = 0,
        detailReads = 0,
        writes = 0;
    page.on("request", (request) => {
        if (request.method() === "POST" && request.url().includes(data.endpoint)) writes += 1;
    });
    await page.route(`**${data.endpoint}/history?*`, async (route) => {
        directoryReads += 1;
        if (directoryFail)
            await route.fulfill({ status: 503, json: { detail: { code: "credit_integrity" } } });
        else await route.continue();
    });
    await page.route(`**${data.endpoint}/revisions/${revision.revision_id}`, async (route) => {
        detailReads += 1;
        if (detailFail)
            await route.fulfill({
                status: 503,
                json: { detail: { code: "credit_revision_integrity" } },
            });
        else await route.continue();
    });
    await button(page, "View version history").click();
    await expect(history(page).getByRole("alert")).toContainText("could not be read safely");
    await shots(page, info, "directory-read-failure", narrow, async (locale) => {
        await expect(history(page, locale).getByRole("alert")).toBeVisible();
        await expect(history(page, locale).locator(".master-records > li")).toHaveCount(0);
        expect(directoryReads).toBe(1);
    });
    directoryFail = false;
    await button(page, "Reload history").click();
    await expect(history(page).locator(".master-records > li")).toHaveCount(2);
    expect(directoryReads).toBe(2);
    await history(page)
        .locator(".master-records > li")
        .filter({ hasText: "Group version: 2 ·" })
        .getByRole("button", { name: "View fixed details", exact: true })
        .click();
    await expect(history(page).getByRole("alert")).toContainText("could not be read safely");
    await shots(page, info, "fixed-read-failure", narrow, async (locale) => {
        await expect(history(page, locale).getByRole("alert")).toBeVisible();
        await expect(history(page, locale).getByRole("heading", { level: 4 })).toHaveCount(0);
        await expect(
            button(page, locale === "zh" ? "返回版本目录" : "Back to versions"),
        ).toBeVisible();
        expect(detailReads).toBe(1);
        expect(directoryReads).toBe(2);
    });
    detailFail = false;
    await button(page, "Reload history").click();
    await expect(history(page).getByRole("heading", { level: 4 })).toContainText(
        "Group version: 2",
    );
    await expect(history(page).getByText(revision.revision_id, { exact: true })).toBeVisible();
    expect(detailReads).toBe(2);
    expect(writes).toBe(0);
    expect(await api(page, "GET", `${data.endpoint}/revisions/${revision.revision_id}`)).toEqual(
        revision,
    );
    expect(await api(page, "GET", data.endpoint)).toEqual(data.original);
});

test("fictional real second history page and its fixed selection survive language switches", async ({
    page,
}, info) => {
    const data = await setup(page);
    let last: CreditRevisionReceipt | null = null;
    // The original plus 25 real single-member revisions is the smallest non-empty second page.
    // This is saved server history, not fabricated pagination metadata.
    for (let previous = 1; previous <= 25; previous += 1) {
        last = await api<CreditRevisionReceipt>(page, "POST", `${data.endpoint}/corrections`, {
            expected_version: previous,
            reason: `Fictional directory version ${previous + 1}`,
            replacement: data.replacement,
        });
        expect(last.version).toBe(previous + 1);
    }
    const reads: string[] = [];
    page.on("request", (request) => {
        const url = new URL(request.url());
        if (
            request.method() === "GET" &&
            (url.pathname === `${data.endpoint}/history` ||
                url.pathname.startsWith(`${data.endpoint}/revisions/`))
        )
            reads.push(url.pathname + url.search);
    });
    await openCurrent(page, data.entity.id);
    await button(page, "View version history").click();
    await expect(history(page).locator(".master-records > li")).toHaveCount(25);
    await button(page, "Next page").click();
    await expect(history(page).locator(".master-records > li")).toHaveCount(1);
    await expect(history(page)).toContainText("Group version: 26");
    expect(reads).toEqual([
        `${data.endpoint}/history?limit=25&offset=0`,
        `${data.endpoint}/history?limit=25&offset=25`,
    ]);
    await shots(page, info, "second-history-page", narrow, async (locale) => {
        await expect(history(page, locale).locator(".master-records > li")).toHaveCount(1);
        await expect(history(page, locale)).toContainText(
            locale === "zh" ? "组版本: 26" : "Group version: 26",
        );
        await expect(button(page, locale === "zh" ? "下一页" : "Next page")).toBeDisabled();
        await expect(button(page, locale === "zh" ? "上一页" : "Previous page")).toBeEnabled();
        expect(reads).toHaveLength(2);
    });
    await button(page, "View fixed details").click();
    await expect(history(page).getByText(last!.revision_id, { exact: true })).toBeVisible();
    await shots(page, info, "selected-history-version", narrow, async (locale) => {
        await expect(history(page, locale).getByRole("heading", { level: 4 })).toContainText(
            locale === "zh" ? "组版本: 26" : "Group version: 26",
        );
        await expect(
            history(page, locale).getByText(last!.revision_id, { exact: true }),
        ).toBeVisible();
        expect(reads).toHaveLength(3);
    });
    await button(page, "Back to versions").click();
    await expect(history(page).locator(".master-records > li")).toHaveCount(1);
    expect(reads.at(-1)).toBe(`${data.endpoint}/history?limit=25&offset=25`);
    await button(page, "Previous page").click();
    await expect(history(page).locator(".master-records > li")).toHaveCount(25);
    expect(reads.at(-1)).toBe(`${data.endpoint}/history?limit=25&offset=0`);
    expect(await api(page, "GET", `${data.endpoint}/revisions/${last!.revision_id}`)).toEqual(last);
});

test("fictional abandoned history request is cancelled before an old 401 can affect the editor", async ({
    page,
}, info) => {
    const data = await setup(page);
    await openCurrent(page, data.entity.id);
    let release!: () => void, reached!: () => void, finished!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    const intercepted = new Promise<void>((resolve) => {
        reached = resolve;
    });
    const handled = new Promise<void>((resolve) => {
        finished = resolve;
    });
    let delay = true,
        reads = 0;
    await page.route(`**${data.endpoint}/history?*`, async (route) => {
        reads += 1;
        if (!delay) return route.continue();
        delay = false;
        reached();
        await gate;
        try {
            await route.fulfill({
                status: 401,
                json: { detail: { code: "authentication_required" } },
            });
        } finally {
            finished();
        }
    });
    const cancelled = page.waitForEvent("requestfailed", {
        predicate: (request) => new URL(request.url()).pathname === `${data.endpoint}/history`,
    });
    try {
        await button(page, "View version history").click();
        await intercepted;
        await expect(history(page).getByRole("status")).toHaveText("Loading…");
        await button(page, "Back to current state").click();
        const form = await openEditor(page);
        release();
        const request = await cancelled;
        await handled;
        expect(request.failure()?.errorText).toMatch(/abort|cancel/iu);
        // The browser observed cancellation, not a delivered 401. Ignored-abort late transport
        // success/error/401 is separately proved by credit-revision-history-read unit tests.
        await expect(page.getByTestId("current-username")).toHaveText(username);
        await expect(button(page, "Sign out")).toBeVisible();
        await expect(form.getByLabel(reasonLabel, { exact: true })).toHaveValue(retainedReason);
        await expect(form.getByLabel(reasonLabel, { exact: true })).toBeEnabled();
        await expect(page.getByRole("alert")).toHaveCount(0);
        await shots(page, info, "abandoned-history-editor", narrow, async (locale) => {
            await expect(
                page.getByLabel(locale === "zh" ? "本次更正或取消的原因" : reasonLabel, {
                    exact: true,
                }),
            ).toHaveValue(retainedReason);
            await expect(page.getByTestId("current-username")).toHaveText(username);
            await expect(button(page, locale === "zh" ? "退出登录" : "Sign out")).toBeVisible();
            expect(reads).toBe(1);
        });
        await button(page, "Discard edits and return to current state").click();
        await button(page, "View version history").click();
        await expect(history(page).locator(".master-records > li")).toHaveCount(1);
        await expect(history(page).getByRole("alert")).toHaveCount(0);
        expect(reads).toBe(2);
        expect((await api<CreditCurrent>(page, "GET", `${data.endpoint}/current`)).version).toBe(1);
    } finally {
        release();
    }
});

test("fictional precision and empty net or tax are rejected without POST or rewriting input", async ({
    page,
}, info) => {
    const data = await setup(page);
    await openCurrent(page, data.entity.id);
    const form = await openEditor(page);
    const net = form.getByLabel("Replacement net", { exact: true });
    const tax = form.getByLabel("Replacement tax", { exact: true });
    let writes = 0;
    page.on("request", (request) => {
        if (request.method() === "POST" && request.url().includes(data.endpoint)) writes += 1;
    });
    for (const [netValue, taxValue] of [
        ["20.001", "3.00"],
        ["20.00", "3.001"],
        ["", "3.00"],
        ["20.00", ""],
    ]) {
        await net.fill(netValue);
        await tax.fill(taxValue);
        await reviewed(page).check();
        await button(page, "Confirm correction").click();
        await expect(form.getByRole("alert")).toBeVisible();
        await expect(net).toHaveValue(netValue);
        await expect(tax).toHaveValue(taxValue);
        await expect(form.getByLabel(reasonLabel, { exact: true })).toHaveValue(retainedReason);
        expect(writes).toBe(0);
    }
    await shots(page, info, "invalid-empty-tax", narrow, async (locale) => {
        const editor = page.getByRole("form", {
            name: locale === "zh" ? "更正贷项组" : "Correct credit group",
            exact: true,
        });
        await expect(editor.getByRole("alert")).toBeVisible();
        await expect(
            editor.getByLabel(locale === "zh" ? "替代净额" : "Replacement net", { exact: true }),
        ).toHaveValue("20.00");
        await expect(
            editor.getByLabel(locale === "zh" ? "替代税额" : "Replacement tax", { exact: true }),
        ).toHaveValue("");
        expect(writes).toBe(0);
    });
    await tax.fill("3.00");
    await reviewed(page).check();
    await expect(form.getByRole("alert")).toHaveCount(0);
    await expect(button(page, "Confirm correction")).toBeEnabled();
    expect(writes).toBe(0);
    expect((await api<CreditCurrent>(page, "GET", `${data.endpoint}/current`)).version).toBe(1);
    expect(await api(page, "GET", data.endpoint)).toEqual(data.original);
});
