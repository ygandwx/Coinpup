import { expect, test, type Page } from "@playwright/test";
import { api, expectNoOverflow, login } from "./helpers";
import { seed, workerOutcome } from "./ocr-helpers";
import { parseAmount } from "../src/money";
import type { Account, Category, OperationState } from "../src/ledger-api";
import type { OcrJob, DuplicateConfirmation, ReviewView } from "../src/ocr-api";

async function postRow(page: Page, id: string, account: string, category: string) {
    await page.getByTestId(`ocr-draft-${id}`).click();
    for (const field of ["Amount", "Currency", "Date"])
        await page.getByLabel(`${field} · Reviewed`, { exact: true }).check();
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
    await page.getByRole("button", { name: "Use reviewed candidates", exact: true }).click();
    const amount = await page.getByLabel("Amount", { exact: true }).inputValue();
    await page.getByLabel("Account", { exact: true }).selectOption(account);
    await page.getByLabel("Recognition date", { exact: true }).fill("2031-07-18");
    await page.getByLabel("Category", { exact: true }).selectOption(category);
    await page.getByLabel("Split amount", { exact: true }).fill(amount);
    await page.getByRole("button", { name: "Save prepared entry", exact: true }).click();
    const response = page.waitForResponse((result) =>
        result.url().endsWith(`/ocr-drafts/${id}/confirmations`),
    );
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    expect((await response).status()).toBe(201);
}

test("missing statement row remains unposted; a failed rescan retries without duplicating confirmed rows", async ({
    page,
}, info) => {
    await page.addInitScript(() => localStorage.setItem("coinpup.locale", "en"));
    await login(page);
    const fixture = seed(false, undefined, 3, true),
        base = `/api/v1/ledgers/${fixture.ledger}`;
    const account = await api<Account>(page, "POST", `${base}/accounts`, {
        name: "Fictional rescan bank",
        kind: "bank",
        asset_ids: ["USD"],
    });
    const category = await api<Category>(page, "POST", `${base}/categories`, {
        name: "Fictional rescan supplies",
        kind: "expense",
    });
    const total = async () =>
        (await api<OperationState[]>(page, "GET", `${base}/operations`)).reduce((sum, row) => {
            if (row.latest_posting.kind !== "expense")
                throw new Error("Expected fictional expense");
            return sum + parseAmount(row.latest_posting.amount, 2);
        }, 0n);
    await page.goto(`/?entity=${fixture.entity}&view=ocr`);
    await postRow(page, fixture.drafts[0], account.id, category.id);
    await postRow(page, fixture.drafts[1], account.id, category.id);
    expect(await total()).toBe(3000n);
    await page.getByTestId(`ocr-draft-${fixture.drafts[2]}`).click();
    await expect(page.getByText("Candidate: Missing", { exact: true })).toBeVisible();
    await expect(page.getByText("Original page 1, table 1, row 3", { exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Confirm and post", exact: true }),
    ).toBeDisabled();
    await page.getByLabel("Choose entry type", { exact: true }).selectOption("expense");
    await page.getByRole("button", { name: "Prepare entry", exact: true }).click();
    await expect(page.getByLabel("Amount", { exact: true })).toHaveValue("");
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByLabel("Uploaded original", { exact: true }).selectOption(fixture.file);
    const created = page.waitForResponse(
        (response) =>
            response.request().method() === "POST" && response.url().endsWith("/ocr-jobs"),
    );
    await page
        .getByRole("button", { name: "Start recognition / retry original request", exact: true })
        .click();
    const createdResponse = await created;
    expect(createdResponse.status()).toBe(201);
    const job = (await createdResponse.json()) as OcrJob;
    expect(workerOutcome(job.id, "fail").state).toBe("failed");
    await page.getByRole("button", { name: "Refresh status", exact: true }).click();
    await expect(page.locator(`[data-job-id="${job.id}"]`)).toContainText("Failed");
    const retried = page.waitForResponse((response) =>
        response.url().endsWith(`/ocr-jobs/${job.id}/retries`),
    );
    await page.getByRole("button", { name: "Retry recognition", exact: true }).click();
    expect((await retried).status()).toBe(200);
    const done = workerOutcome(job.id, "finish");
    expect(done.state).toBe("succeeded");
    await page.getByRole("button", { name: "Refresh status", exact: true }).click();
    await page.getByTestId(`ocr-draft-${done.drafts[0]}`).click();
    await expect(page.getByText(/This source row was already confirmed/u)).toBeVisible();
    for (const id of done.drafts.slice(0, 2)) {
        const duplicates = await api<DuplicateConfirmation[]>(
            page,
            "GET",
            `${base}/ocr-drafts/${id}/duplicates`,
        );
        expect(duplicates).toHaveLength(1);
    }
    expect(await total()).toBe(3000n);
    await postRow(page, done.drafts[2], account.id, category.id);
    await expect.poll(total).toBe(6000n);
    expect(await api<OperationState[]>(page, "GET", `${base}/operations`)).toHaveLength(3);
    const finalJob = await api<OcrJob>(page, "GET", `${base}/ocr-jobs/${job.id}`);
    expect(finalJob.attempts).toBe(2);
    const untouched = await api<ReviewView>(
        page,
        "GET",
        `${base}/ocr-drafts/${fixture.drafts[2]}/review`,
    );
    expect(untouched.status).toBe("draft");
    expect(
        untouched.fields.find((field) => field.path.endsWith(".amount"))?.candidate_value,
    ).toBeNull();
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        for (const language of ["zh", "en"]) {
            await page
                .getByRole("button", { name: language === "zh" ? "中文" : "English", exact: true })
                .click();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`ocr-rescan-${language}-${width}.png`),
                fullPage: true,
            });
        }
    }
});
