import { randomUUID } from "node:crypto";
import { expect, type Page, type TestInfo } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { BusinessDraft } from "../src/business-draft-api";
import type {
    BusinessConfirmationDetail,
    BusinessConfirmationReceipt,
} from "../src/business-confirmation-api";
import type {
    CreditCreate,
    CreditDetail,
    CreditReceipt,
    CreditRevisionReceipt,
} from "../src/credit-api";
import type { Account, Balance } from "../src/ledger-api";

export const button = (page: Page, name: string) => page.getByRole("button", { name, exact: true });
export const lineName = (letter: string) => `Fictional revision source ${letter} 中文`;
export const revisionReason = "  Fictional complete correction 中文 🧾  ";
export const replacementReason = "Fictional replacement facts 中文";
export const reviewed = (page: Page, locale = "en") =>
    page.getByRole("checkbox", {
        name: locale === "zh" ? "我已核对整组旧事实" : "I reviewed the complete previous group",
        exact: false,
    });

export async function fixedPost<T>(
    page: Page,
    path: string,
    raw: string,
    key: string,
    status: number,
) {
    const session = await page.request.get("/api/v1/auth/session");
    expect(session.status()).toBe(200);
    const response = await page.request.post(path, {
        data: raw,
        headers: {
            "Content-Type": "application/json",
            Origin: new URL(page.url()).origin,
            "X-CSRF-Token": (await session.json()).csrf_token,
            "Idempotency-Key": key,
        },
    });
    expect(response.status(), await response.text()).toBe(status);
    return response.json() as Promise<T>;
}

export async function revisionSetup(page: Page, kind: "invoice" | "bill", long = false) {
    const data = await confirmationSetup(page, kind);
    // Replace only this disposable, unconfirmed draft; preserve existing shared fixtures.
    const source = await api<BusinessDraft>(page, "PUT", data.base, {
        expected_version: data.source.version,
        document_kind: kind,
        party_id: data.party.id,
        asset_id: "USD",
        issue_date: "2026-01-02",
        due_date: "2026-02-01",
        notes: "Fictional complete group browser evidence 中文",
        lines: (long ? ["A"] : ["A", "B", "C"]).map((letter) => ({
            id: randomUUID(),
            description: lineName(letter),
            quantity: "1",
            unit_price: long ? "90000000000000000000" : "100",
            tax_rate_percent: "10",
            category_id: data.source.lines[0].category_id,
            recognition_date: "2026-01-02",
        })),
    });
    const confirmation = await api<BusinessConfirmationReceipt>(
        page,
        "POST",
        `${data.base}/confirm`,
        {
            id: randomUUID(),
            expected_version: source.version,
        },
    );
    const confirmationDetail = await api<BusinessConfirmationDetail>(
        page,
        "GET",
        `${data.base}/confirmation`,
    );
    expect(confirmationDetail.confirmation).toEqual(confirmation);
    const account = await api<Account>(page, "POST", `${data.ledger}/accounts`, {
        name: "Fictional revision cash remains unchanged",
        kind: "bank",
        asset_ids: ["USD"],
    });
    await api(page, "POST", `${data.ledger}/opening-balances`, {
        account_id: account.id,
        asset_id: "USD",
        amount: "1000.00",
        transaction_date: "2026-01-01",
        description: "Fictional revision opening cash",
    });
    const balances = await api<Balance[]>(page, "GET", `${data.ledger}/balances?limit=100`);
    expect(balances).toHaveLength(1);
    expect(balances[0]).toMatchObject({
        account_id: account.id,
        asset_id: "USD",
        amount: "1000.00",
    });
    const id = randomUUID(),
        key = randomUUID();
    const body: CreditCreate = {
        id,
        document_id: source.id,
        expected_version: confirmation.document_version,
        credit_date: "2026-01-05",
        reason: "Fictional original A/B facts 中文",
        lines: source.lines.slice(0, long ? 1 : 2).map((line) => ({
            source_line_id: line.id,
            net_amount: "17.23",
            tax_amount: "2.77",
            recognition_date: "2026-01-05",
        })),
    };
    const raw = JSON.stringify(body),
        path = `${data.ledger}/business-credits`;
    const original = await fixedPost<CreditReceipt>(page, path, raw, key, 201);
    const endpoint = `${path}/${id}`;
    const detail = await api<CreditDetail>(page, "GET", endpoint);
    expect(detail.receipt).toEqual(original);
    return {
        ...data,
        source,
        confirmation,
        confirmationDetail,
        balances,
        original,
        detail,
        endpoint,
        raw,
        key,
        path,
    };
}

export async function openCurrent(page: Page, entityId: string) {
    await page.goto(`/?entity=${entityId}&view=business-credits`);
    await button(page, "Credit history").click();
    await button(page, "View credit details").click();
    await button(page, "Current state and credit revisions").click();
    await expect(
        page.getByRole("heading", { name: "Current credit group", exact: true }),
    ).toBeVisible();
}

export async function editReplacement(page: Page, letters: string[], net = "20.00", tax = "3.00") {
    await button(page, "Correct complete credit group").click();
    const form = page.getByRole("form", { name: "Correct credit group", exact: true });
    await expect(form).toBeVisible();
    await form
        .getByLabel("Reason for this correction or cancellation", { exact: true })
        .fill(revisionReason);
    await form
        .getByLabel("Replacement credit transaction date", { exact: true })
        .fill("2026-01-06");
    await form.getByLabel("Replacement credit reason", { exact: true }).fill(replacementReason);
    // Explicitly select the complete replacement in its intended order.
    for (const checkbox of await form
        .getByRole("checkbox", { name: "Include in the complete replacement", exact: true })
        .all())
        await checkbox.uncheck();
    for (const letter of letters) {
        const row = form.getByRole("group", { name: lineName(letter), exact: true });
        await row
            .getByRole("checkbox", { name: "Include in the complete replacement", exact: true })
            .check();
        await row.getByLabel("Replacement net", { exact: true }).fill(net);
        await row.getByLabel("Replacement tax", { exact: true }).fill(tax);
        await row.getByLabel("Replacement recognition date", { exact: true }).fill("2026-01-06");
    }
    await reviewed(page).check();
}

export async function submitRevision(page: Page, endpoint: string, action: "correct" | "cancel") {
    const path = `${endpoint}/${action === "correct" ? "corrections" : "cancellations"}`;
    const response = page.waitForResponse(
        (value) => new URL(value.url()).pathname === path && value.request().method() === "POST",
    );
    await button(
        page,
        action === "correct" ? "Confirm correction" : "Confirm credit cancellation",
    ).click();
    const saved = await response;
    expect(saved.status(), await saved.text()).toBe(200);
    const receipt = (await saved.json()) as CreditRevisionReceipt;
    await expect(
        page.getByText(
            "This revision is saved and its complete transition receipt is verified. This receipt does not represent today's current state.",
            { exact: true },
        ),
    ).toBeVisible();
    return {
        receipt,
        raw: saved.request().postData()!,
        key: saved.request().headers()["idempotency-key"],
        path,
    };
}

export async function shots(
    page: Page,
    info: TestInfo,
    label: string,
    matrix: readonly (readonly ["zh" | "en", number])[],
    check?: (locale: "zh" | "en") => Promise<void>,
) {
    for (const [locale, width] of matrix) {
        await page.setViewportSize({ width, height: 1000 });
        await button(page, locale === "zh" ? "中文" : "English").click();
        if (check) await check(locale);
        await expectNoOverflow(page);
        await page.screenshot({
            path: info.outputPath(`credit-revision-${label}-${locale}-${width}.png`),
            fullPage: true,
        });
    }
    await button(page, "English").click();
}

export async function closeReceipt(page: Page) {
    await button(page, "Close this revision receipt").click();
    await button(page, "Current state and credit revisions").click();
    await expect(
        page.getByRole("heading", { name: "Current credit group", exact: true }),
    ).toBeVisible();
}

export async function historyDetail(page: Page, version: number) {
    const history = page.getByRole("region", { name: "Credit version history", exact: true });
    const row = history
        .locator(".master-records > li")
        .filter({ hasText: `Group version: ${version} ·` });
    await row.getByRole("button", { name: "View fixed details", exact: true }).click();
    await expect(history.getByRole("heading", { level: 4 })).toContainText(
        `Group version: ${version}`,
    );
    return history;
}
