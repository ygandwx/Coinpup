import { expect, test, type Locator, type Page } from "@playwright/test";
import type {
    DocumentFile as FileRecord,
    OperationFileLink as FileLink,
    UploadCompletion as Completion,
    UploadReservation as UploadState,
} from "../src/document-api";
import type {
    Account,
    Balance,
    Category,
    Entity,
    HistoryEntry,
    OperationResponse,
    OperationState,
} from "../src/ledger-api";
import { api, expectNoOverflow, fictionalName, login, selectLedger, username } from "./helpers";

type Fixture = { entity: Entity; base: string; operation?: OperationResponse };

// A real minimal PDF containing only explicitly fictional text, plus a one-pixel PNG.
function fictionalPdf(): Buffer {
    const content = "BT /F1 12 Tf 50 100 Td (Fictional Coinpup browser receipt) Tj ET";
    const objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        `<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}\nendstream`,
    ];
    let output = "%PDF-1.4\n";
    const offsets = [0];
    for (const [index, body] of objects.entries()) {
        offsets.push(Buffer.byteLength(output));
        output += `${index + 1} 0 obj\n${body}\nendobj\n`;
    }
    const xref = Buffer.byteLength(output);
    output += "xref\n0 6\n0000000000 65535 f \n";
    output += offsets
        .slice(1)
        .map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)
        .join("");
    return Buffer.from(output + `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
}

const PDF = fictionalPdf();
const PNG = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1kAAAAASUVORK5CYII=",
    "base64",
);

test.beforeEach(async ({ context }) => {
    await context.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
});

async function fixture(page: Page, expense = false): Promise<Fixture> {
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("documents ledger"),
        base_asset_id: "USD",
        template_key: "personal_default",
        locale: "en",
    });
    const base = `/api/v1/ledgers/${entity.ledger.id}`;
    if (!expense) return { entity, base };
    const account = await api<Account>(page, "POST", base + "/accounts", {
        name: fictionalName("evidence wallet"),
        kind: "cash",
        asset_ids: ["USD"],
    });
    await api(page, "POST", base + "/opening-balances", {
        account_id: account.id,
        asset_id: "USD",
        amount: "1000.00",
        transaction_date: "2026-01-01",
        description: "Fictional documents opening",
    });
    const category = (await api<Category[]>(page, "GET", base + "/categories")).find(
        (item) => item.kind === "expense",
    )!;
    const operation = await api<OperationResponse>(page, "POST", base + "/expenses", {
        account_id: account.id,
        asset_id: "USD",
        amount: "100.00",
        transaction_date: "2026-10-03",
        recognition_date: "2026-10-03",
        description: "Fictional purchase needing evidence",
        splits: [{ category_id: category.id, amount: "100.00" }],
    });
    return { entity, base, operation };
}

async function showDocuments(page: Page, data: Fixture): Promise<void> {
    await page.goto("/");
    await expect(page.getByTestId("current-username")).toHaveText(username);
    await selectLedger(page, data.entity.name);
    await page.getByRole("button", { name: "Documents", exact: true }).click();
    await expect(page.getByLabel("File to upload", { exact: true })).toBeVisible();
}

async function chooseFile(page: Page, bytes: Buffer, name: string): Promise<void> {
    await page
        .getByLabel("File to upload", { exact: true })
        .setInputFiles({
            name,
            mimeType: name.endsWith(".png") ? "image/png" : "application/pdf",
            buffer: bytes,
        });
}

function contentRequest(url: string, data: Fixture): boolean {
    const pathname = new URL(url).pathname;
    return pathname.startsWith(data.base + "/uploads/") && pathname.endsWith("/content");
}

async function upload(page: Page, data: Fixture, bytes: Buffer, name: string): Promise<Completion> {
    await chooseFile(page, bytes, name);
    const responsePromise = page.waitForResponse(
        (response) => response.request().method() === "PUT" && contentRequest(response.url(), data),
    );
    await page.getByRole("button", { name: "Upload file", exact: true }).click();
    const response = await responsePromise;
    expect(response.status(), await response.text()).toBe(200);
    const completed = (await response.json()) as Completion;
    await expect(page.getByText(/^Upload confirmed\./)).toBeVisible();
    await expect(page.getByTestId(`file-${completed.file_id}`)).toBeVisible();
    return completed;
}

async function download(page: Page, row: Locator, bytes: Buffer, name: string): Promise<void> {
    await downloadFromButton(
        page,
        row.getByRole("button", { name: "Download", exact: true }),
        bytes,
        name,
    );
}

async function downloadFromButton(
    page: Page,
    button: Locator,
    bytes: Buffer,
    name: string,
): Promise<void> {
    const pending = page.waitForEvent("download");
    await button.click();
    const actual = await pending;
    expect(actual.suggestedFilename()).toBe(name);
    const stream = await actual.createReadStream();
    if (!stream) throw new Error("The real private download did not provide a byte stream");
    const chunks: Buffer[] = [];
    for await (const chunk of stream) chunks.push(Buffer.from(chunk));
    expect(Buffer.concat(chunks)).toEqual(bytes);
}

async function files(page: Page, data: Fixture): Promise<FileRecord[]> {
    return api(page, "GET", data.base + "/files?include_archived=true&limit=100&offset=0");
}

async function balances(page: Page, data: Fixture): Promise<Balance[]> {
    return api(page, "GET", data.base + "/balances?limit=100&offset=0");
}

async function clickMutation<T>(
    page: Page,
    button: Locator,
    path: string,
    method: string,
): Promise<T> {
    const pending = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === path && response.request().method() === method,
    );
    await button.click();
    const response = await pending;
    expect(response.status(), await response.text()).toBe(200);
    return response.json() as Promise<T>;
}

async function sameOwnerLogin(page: Page): Promise<void> {
    const password = process.env.E2E_PASSWORD;
    if (!password) throw new Error("Set a disposable E2E_PASSWORD");
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByTestId("current-username")).toHaveText(username);
}

test("real PDF and PNG downloads retain bytes and duplicate detection stays inside each ledger", async ({
    page,
    browser,
}, testInfo) => {
    test.setTimeout(60_000);
    await login(page);
    const first = await fixture(page);
    const other = await fixture(page);
    await showDocuments(page, first);
    const pdfName = "Fictional receipt 票据.pdf";
    const pdf = await upload(page, first, PDF, pdfName);
    const png = await upload(page, first, PNG, "Fictional photograph.png");
    expect(pdf.duplicate).toBe(false);
    expect(png.media_type).toBe("image/png");
    expect(pdf.byte_size).toBe(PDF.length);
    await download(page, page.getByTestId(`file-${pdf.file_id}`), PDF, pdfName);
    await download(page, page.getByTestId(`file-${png.file_id}`), PNG, "Fictional photograph.png");
    const duplicate = await upload(page, first, PDF, pdfName);
    expect(duplicate.duplicate).toBe(true);
    expect(duplicate.file_id).toBe(pdf.file_id);
    await expect(
        page.getByText(
            "Upload confirmed. Exact file already exists in this ledger; the original was reused.",
            { exact: true },
        ),
    ).toBeVisible();
    expect(await files(page, first)).toHaveLength(2);
    await expect(page.getByTestId(`file-${pdf.file_id}`)).toHaveCount(1);
    await clickMutation(
        page,
        page
            .getByTestId(`file-${pdf.file_id}`)
            .getByRole("button", { name: "Archive file", exact: true }),
        `${first.base}/files/${pdf.file_id}`,
        "PATCH",
    );
    await expect(page.getByLabel("Show archived files", { exact: true })).not.toBeChecked();
    await expect(page.getByTestId(`file-${pdf.file_id}`)).toHaveCount(0);
    await chooseFile(page, PDF, pdfName);
    const archivedDuplicateResponse = page.waitForResponse(
        (response) =>
            response.request().method() === "PUT" && contentRequest(response.url(), first),
    );
    await page.getByRole("button", { name: "Upload file", exact: true }).click();
    const archivedResponse = await archivedDuplicateResponse;
    expect(archivedResponse.status(), await archivedResponse.text()).toBe(200);
    const archivedDuplicate = (await archivedResponse.json()) as Completion;
    expect(archivedDuplicate.duplicate).toBe(true);
    expect(archivedDuplicate.file_id).toBe(pdf.file_id);
    await expect(
        page.getByText(
            "Upload confirmed. Exact file already exists in this ledger; the original was reused.",
            { exact: true },
        ),
    ).toBeVisible();
    const catalog = await files(page, first);
    expect(catalog).toHaveLength(2);
    expect(catalog.find((file) => file.id === pdf.file_id)?.archived).toBe(true);
    await downloadFromButton(
        page,
        page.getByRole("button", { name: "Download saved file", exact: true }),
        PDF,
        pdfName,
    );
    await selectLedger(page, other.entity.name);
    const isolated = await upload(page, other, PDF, pdfName);
    expect(isolated.duplicate).toBe(false);
    expect(isolated.file_id).not.toBe(pdf.file_id);
    expect(isolated.sha256).toBe(pdf.sha256);
    expect(await files(page, other)).toHaveLength(1);
    await expect(page.getByTestId(`file-${pdf.file_id}`)).toHaveCount(0);
    const crossed = await page.request.get(`${other.base}/files/${pdf.file_id}/content`);
    expect(crossed.status()).toBe(404);
    expect(await crossed.text()).not.toContain(pdfName);
    const anonymous = await browser.newContext();
    try {
        const denied = await anonymous.request.get(
            new URL(`${first.base}/files/${pdf.file_id}/content`, page.url()).href,
        );
        expect(denied.status()).toBe(401);
        expect(await denied.body()).not.toEqual(PDF);
    } finally {
        await anonymous.close();
    }
    for (const data of [first, other])
        expect(await api<OperationState[]>(page, "GET", data.base + "/operations")).toEqual([]);
    await expectNoOverflow(page);
    await page.screenshot({ path: testInfo.outputPath("private-documents.png"), fullPage: true });
});

test("file title conflicts retain drafts and evidence links survive cancellation without posting money", async ({
    page,
    context,
}) => {
    test.setTimeout(90_000);
    await login(page);
    const data = await fixture(page, true);
    const operation = data.operation!;
    const operationPath = `${data.base}/operations/${operation.id}`;
    await showDocuments(page, data);
    const receipt = await upload(page, data, PDF, "Fictional linked receipt.pdf");
    const filePath = `${data.base}/files/${receipt.file_id}`;
    const second = await context.newPage();
    await showDocuments(second, data);
    for (const window of [page, second])
        await window
            .getByTestId(`file-${receipt.file_id}`)
            .getByRole("button", { name: "Edit title", exact: true })
            .click();
    await page.getByLabel("File title", { exact: true }).fill("Fictional first title");
    await second.getByLabel("File title", { exact: true }).fill("Fictional preserved draft");
    const winner = await clickMutation<FileRecord>(
        page,
        page.getByRole("button", { name: "Save title", exact: true }),
        filePath,
        "PATCH",
    );
    expect(winner.version).toBe(2);
    const conflictPromise = second.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === filePath &&
            response.request().method() === "PATCH",
    );
    await second.getByRole("button", { name: "Save title", exact: true }).click();
    const conflict = await conflictPromise;
    expect(conflict.status()).toBe(409);
    expect(conflict.request().postDataJSON().expected_version).toBe(1);
    await expect(second.getByRole("alert")).toBeVisible();
    await expect(second.getByLabel("File title", { exact: true })).toHaveValue(
        "Fictional preserved draft",
    );
    await second.getByRole("button", { name: "中文", exact: true }).click();
    await expect(second.getByRole("textbox")).toHaveValue("Fictional preserved draft");
    await second.getByRole("button", { name: "English", exact: true }).click();
    await second.getByRole("button", { name: "Reload current file", exact: true }).click();
    await expect(second.getByLabel("File title", { exact: true })).toHaveValue(
        "Fictional first title",
    );
    await second.getByLabel("File title", { exact: true }).fill("Fictional resolved title");
    const resolved = await clickMutation<FileRecord>(
        second,
        second.getByRole("button", { name: "Save title", exact: true }),
        filePath,
        "PATCH",
    );
    expect(resolved.version).toBe(3);
    await second.close();
    const before = {
        balances: await balances(page, data),
        history: await api<HistoryEntry[]>(page, "GET", operationPath + "/history"),
    };
    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await page
        .getByTestId(`operation-${operation.id}`)
        .getByRole("button", { name: "Files", exact: true })
        .click();
    await page.getByLabel("Existing file", { exact: true }).selectOption(receipt.file_id);
    const linkPath = operationPath + `/files/${receipt.file_id}`;
    const link = await clickMutation<FileLink>(
        page,
        page.getByRole("button", { name: "Link file", exact: true }),
        linkPath,
        "PUT",
    );
    await expect(page.getByTestId(`file-link-${link.id}`)).toBeVisible();
    expect(await balances(page, data)).toEqual(before.balances);
    expect(await api<HistoryEntry[]>(page, "GET", operationPath + "/history")).toEqual(
        before.history,
    );
    // Replay the same real association without requiring the UI to offer an already linked file.
    const session = await (await page.request.get("/api/v1/auth/session")).json();
    const repeated = await page.request.put(linkPath, {
        headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": session.csrf_token },
    });
    expect(repeated.status()).toBe(200);
    expect(await repeated.json()).toEqual(link);
    await page.getByRole("button", { name: "Back to transactions", exact: true }).click();
    await page
        .getByTestId(`operation-${operation.id}`)
        .getByRole("button", { name: "Cancel entry", exact: true })
        .click();
    await page
        .getByLabel("Revision reason", { exact: true })
        .fill("Fictional evidence retention cancellation");
    await clickMutation(
        page,
        page.getByRole("button", { name: "Confirm cancellation", exact: true }),
        operationPath + "/cancellations",
        "POST",
    );
    await expect(page.getByTestId(`operation-${operation.id}`)).toContainText("Cancelled");
    const cancelledHistory = await api<HistoryEntry[]>(page, "GET", operationPath + "/history");
    expect((await balances(page, data)).map((item) => item.amount)).toEqual(["1000.00"]);
    await page
        .getByTestId(`operation-${operation.id}`)
        .getByRole("button", { name: "Files", exact: true })
        .click();
    await expect(page.getByTestId(`file-link-${link.id}`)).toBeVisible();
    const archived = await clickMutation<FileLink>(
        page,
        page
            .getByTestId(`file-link-${link.id}`)
            .getByRole("button", { name: "Archive link", exact: true }),
        linkPath,
        "PATCH",
    );
    expect(archived.archived).toBe(true);
    await page.getByLabel("Show archived links", { exact: true }).check();
    await clickMutation(
        page,
        page
            .getByTestId(`file-link-${link.id}`)
            .getByRole("button", { name: "Restore link", exact: true }),
        linkPath,
        "PATCH",
    );
    await page.getByRole("button", { name: "Back to transactions", exact: true }).click();
    await page.getByRole("button", { name: "Documents", exact: true }).click();
    await clickMutation(
        page,
        page
            .getByTestId(`file-${receipt.file_id}`)
            .getByRole("button", { name: "Archive file", exact: true }),
        filePath,
        "PATCH",
    );
    await page.getByLabel("Show archived files", { exact: true }).check();
    await download(
        page,
        page.getByTestId(`file-${receipt.file_id}`),
        PDF,
        "Fictional linked receipt.pdf",
    );
    await clickMutation(
        page,
        page
            .getByTestId(`file-${receipt.file_id}`)
            .getByRole("button", { name: "Restore file", exact: true }),
        filePath,
        "PATCH",
    );
    expect(await api<HistoryEntry[]>(page, "GET", operationPath + "/history")).toEqual(
        cancelledHistory,
    );
    expect((await balances(page, data)).map((item) => item.amount)).toEqual(["1000.00"]);
    await api(page, "PATCH", `/api/v1/entities/${data.entity.id}`, {
        expected_version: 1,
        archived: true,
    });
    await page.reload();
    await expect(page.getByTestId("current-username")).toHaveText(username);
    await expect(page.getByRole("button", { name: "Upload file", exact: true })).toBeDisabled();
    await expect(
        page
            .getByTestId(`file-${receipt.file_id}`)
            .getByRole("button", { name: "Edit title", exact: true }),
    ).toBeDisabled();
    await download(
        page,
        page.getByTestId(`file-${receipt.file_id}`),
        PDF,
        "Fictional linked receipt.pdf",
    );
    await expectNoOverflow(page);
});

test("reauthentication retains the original upload and a lost committed response is confirmed by ready status", async ({
    page,
}) => {
    test.setTimeout(60_000);
    await login(page);
    const data = await fixture(page, true);
    await showDocuments(page, data);
    const originalBalances = await balances(page, data);
    const reservePath = data.base + "/uploads";
    const manifests: unknown[] = [];
    const contents: { path: string; bytes: Buffer | null }[] = [];
    let reservation: UploadState | undefined;
    let original: Completion | undefined;
    await page.route(
        (url) => url.pathname === reservePath,
        async (route) => {
            if (route.request().method() !== "POST") {
                await route.continue();
                return;
            }
            manifests.push(route.request().postDataJSON());
            if (manifests.length > 1) {
                await route.continue();
                return;
            }
            const response = await route.fetch();
            expect(response.status(), await response.text()).toBe(201);
            reservation = (await response.json()) as UploadState;
            const session = await (await page.request.get("/api/v1/auth/session")).json();
            const logout = await page.request.post("/api/v1/auth/logout", {
                headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": session.csrf_token },
            });
            expect(logout.status()).toBe(204);
            await route.fulfill({ response });
        },
    );
    await page.route(
        (url) => contentRequest(url.href, data),
        async (route) => {
            contents.push({
                path: new URL(route.request().url()).pathname,
                bytes: route.request().postDataBuffer(),
            });
            if (contents.length === 1) {
                await route.continue();
                return;
            }
            const response = await route.fetch();
            expect(response.status(), await response.text()).toBe(200);
            original = (await response.json()) as Completion;
            await route.abort("connectionfailed");
        },
    );
    const unauthorized = page.waitForResponse(
        (response) => contentRequest(response.url(), data) && response.status() === 401,
    );
    await chooseFile(page, PNG, "Fictional private recovery.png");
    await page.getByRole("button", { name: "Upload file", exact: true }).click();
    expect((await unauthorized).status()).toBe(401);
    await expect(page.getByRole("heading", { name: "Welcome back", exact: true })).toBeVisible();
    await expect(page.getByText("Fictional private recovery.png", { exact: true })).toHaveCount(0);
    expect(contents).toHaveLength(1);
    await sameOwnerLogin(page);
    await expect(page.getByText("Upload not confirmed", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Ledger", { exact: true })).toHaveValue(data.entity.id);
    expect(contents).toHaveLength(1); // A fresh session never automatically resends the file.
    expect(await files(page, data)).toHaveLength(0);
    expect((await api<UploadState>(page, "GET", reservePath + `/${reservation!.id}`)).state).toBe(
        "pending",
    );
    await page.getByRole("button", { name: "Retry original upload", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original upload", exact: true }),
    ).toBeEnabled();
    await expect(page.getByText("Upload not confirmed", { exact: true })).toBeVisible();
    expect(original).toBeDefined();
    expect(contents).toHaveLength(2);
    expect(contents[1]).toEqual(contents[0]);
    expect(contents[0].bytes).toEqual(PNG);
    for (const submitted of manifests) expect(submitted).toEqual(manifests[0]);
    const checked = page.waitForResponse(
        (response) =>
            new URL(response.url()).pathname === reservePath + `/${reservation!.id}` &&
            response.request().method() === "GET",
    );
    await page.getByRole("button", { name: "Check upload status", exact: true }).click();
    const ready = (await (await checked).json()) as UploadState;
    expect(ready.state).toBe("ready");
    expect(ready.response).toEqual(original);
    await expect(page.getByText("Upload confirmed.", { exact: true })).toBeVisible();
    await expect(page.getByTestId(`file-${original!.file_id}`)).toHaveCount(1);
    expect(contents).toHaveLength(2);
    expect(await files(page, data)).toHaveLength(1);
    expect(await balances(page, data)).toEqual(originalBalances);
    expect(await api<OperationState[]>(page, "GET", data.base + "/operations")).toHaveLength(2);
    await download(
        page,
        page.getByTestId(`file-${original!.file_id}`),
        PNG,
        "Fictional private recovery.png",
    );
    await expectNoOverflow(page);
});

test("stopping an unconfirmed upload keeps the pending identity until the original bytes are retried", async ({
    page,
}) => {
    test.setTimeout(60_000);
    await login(page);
    const data = await fixture(page);
    await showDocuments(page, data);
    const submissions: { path: string; bytes: Buffer | null }[] = [];
    let release: (() => void) | undefined;
    await page.route(
        (url) => contentRequest(url.href, data),
        async (route) => {
            submissions.push({
                path: new URL(route.request().url()).pathname,
                bytes: route.request().postDataBuffer(),
            });
            if (submissions.length === 1) {
                // Hold before forwarding. This proves browser-stop recovery, not partial server receipt.
                await new Promise<void>((resolve) => {
                    release = resolve;
                });
                await route.abort("aborted").catch(() => undefined);
            } else await route.continue();
        },
    );
    try {
        const requested = page.waitForRequest((request) => contentRequest(request.url(), data));
        await chooseFile(page, PDF, "Fictional interrupted upload.pdf");
        await page.getByRole("button", { name: "Upload file", exact: true }).click();
        await requested;
        await expect(page.getByRole("button", { name: "Stop upload", exact: true })).toBeVisible();
        await expect.poll(() => submissions.length).toBe(1);
        await page.getByRole("button", { name: "Stop upload", exact: true }).click();
        release?.();
        await expect(page.getByText("Upload not confirmed", { exact: true })).toBeVisible();
        const uploadPath = submissions[0].path.slice(0, -"/content".length);
        const checked = page.waitForResponse(
            (response) =>
                new URL(response.url()).pathname === uploadPath &&
                response.request().method() === "GET",
        );
        await page.getByRole("button", { name: "Check upload status", exact: true }).click();
        expect((await (await checked).json()).state).toBe("pending");
        await expect(
            page.getByRole("button", { name: "Check upload status", exact: true }),
        ).toBeEnabled();
        await expect(page.getByText("Upload not confirmed", { exact: true })).toBeVisible();
        await expect(page.getByLabel("Ledger", { exact: true })).toBeDisabled();
        await expect(page.getByLabel("File to upload", { exact: true })).toBeDisabled();
        expect(await files(page, data)).toHaveLength(0);
        expect(submissions).toHaveLength(1);
        const completed = page.waitForResponse(
            (response) => contentRequest(response.url(), data) && response.status() === 200,
        );
        await page.getByRole("button", { name: "Retry original upload", exact: true }).click();
        const receipt = (await (await completed).json()) as Completion;
        await expect(page.getByText("Upload confirmed.", { exact: true })).toBeVisible();
        expect(submissions).toHaveLength(2);
        expect(submissions[1]).toEqual(submissions[0]);
        expect(submissions[0].bytes).toEqual(PDF);
        expect(receipt.duplicate).toBe(false);
        expect(receipt.upload_id).toBe(uploadPath.split("/").at(-1));
        expect(await files(page, data)).toHaveLength(1);
        await download(
            page,
            page.getByTestId(`file-${receipt.file_id}`),
            PDF,
            "Fictional interrupted upload.pdf",
        );
        await expectNoOverflow(page);
    } finally {
        release?.();
    }
});
