import { randomUUID, createHash } from "node:crypto";
import { test, expect } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import { invoiceSetup } from "./invoice-helpers";
import type { InvoiceProfile, InvoiceComplete } from "../src/invoice-issuance-receipt";
import type { StoredFile, UploadReservation, UploadCompletion } from "../src/files-api";

test("private company profile uses a reviewed logo; stable create, edit, archive, restore and bilingual issue", async ({
    page,
}, info) => {
    const { company, ledger, doc, confirmation } = await invoiceSetup(page);
    const logo = Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMQdtvyHwADigINTnI7cwAAAABJRU5ErkJggg==",
        "base64",
    );
    const u = await api<UploadReservation>(page, "POST", ledger + "/uploads", {
        id: randomUUID(),
        original_filename: "Fictional 公司标志.png",
        declared_size: logo.length,
    });
    const session = await (await page.request.get("/api/v1/auth/session")).json();
    const response = await page.request.put(ledger + "/uploads/" + u.id + "/content", {
        headers: {
            Origin: new URL(page.url()).origin,
            "X-CSRF-Token": session.csrf_token,
            "Content-Type": "application/octet-stream",
        },
        data: logo,
    });
    expect(response.status(), await response.text()).toBe(200);
    const file = (await response.json()) as UploadCompletion;
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    async function captureProfile(prefix: string, selectedLabel?: string) {
        for (const width of [1440, 375, 320])
            for (const locale of ["zh", "en"]) {
                await page.setViewportSize({ width, height: 950 });
                await button(locale === "zh" ? "中文" : "English").click();
                const choice = page.getByRole("combobox", {
                    name: locale === "zh" ? "账本内 Logo 原件" : "Private ledger logo",
                    exact: true,
                });
                await expect(choice).toHaveValue(file.file_id);
                if (selectedLabel)
                    await expect(choice.locator("option:checked")).toHaveText(selectedLabel);
                await expectNoOverflow(page);
                await page.screenshot({
                    path: info.outputPath(`${prefix}-${locale}-${width}.png`),
                    fullPage: true,
                });
            }
    }
    await page.goto(`/?entity=${company.id}&view=invoices`);
    await button("Manage issuance profiles").click();
    await button("Create profile").click();
    const name = "F".repeat(28) + "𠮷".repeat(130),
        editedName = "E".repeat(28) + "𠮷".repeat(132),
        notes = "  Retain exact fictional payment instructions.  ";
    const input = page.getByRole("textbox", { name: "Profile name", exact: true });
    const sent: string[] = [];
    page.on("request", (r) => {
        if (
            new URL(r.url()).pathname.startsWith(ledger + "/invoice-profiles") &&
            ["POST", "PUT"].includes(r.method())
        )
            sent.push(r.postData()!);
    });
    await input.fill(editedName + "末");
    await expect(input).toHaveValue(editedName + "末");
    await expect(input).toHaveAttribute("aria-invalid", "true");
    await expect(button("Save profile")).toBeDisabled();
    await button("中文").click();
    await expect(page.getByText("配置名称不能超过 160 个字符。", { exact: true })).toBeVisible();
    await button("English").click();
    await expect(input).toHaveAccessibleDescription(
        "Use at most 160 characters for the profile name.",
    );
    expect(sent).toEqual([]);
    await input.fill(" " + name + " ");
    await expect(input).toHaveValue(" " + name + " ");
    await page.getByRole("textbox", { name: "Number prefix", exact: true }).fill("演练-");
    await page.getByRole("textbox", { name: "Minimum digits", exact: true }).fill("3");
    await page.getByRole("textbox", { name: "Payment instructions", exact: true }).fill(notes);
    await page
        .getByRole("combobox", { name: "Private ledger logo", exact: true })
        .selectOption(file.file_id);
    await captureProfile("invoice-profile");
    const route = "**" + ledger + "/invoice-profiles",
        writes: string[] = [];
    await page.route(route, async (r) => {
        if (r.request().method() !== "POST") {
            await r.continue();
            return;
        }
        writes.push(r.request().postData()!);
        if (writes.length === 1) {
            const actual = await r.fetch();
            expect(actual.status()).toBe(201);
            await r.abort("failed");
        } else await r.continue();
    });
    await button("Save profile").press("Enter");
    await button("Retry original profile request").click();
    await expect(page.getByText("Profile verified and saved.", { exact: true })).toBeVisible();
    expect(writes).toHaveLength(2);
    expect(writes[1]).toBe(writes[0]);
    const id = JSON.parse(writes[0]).id;
    const path = ledger + "/invoice-profiles/" + id,
        get = () => api<InvoiceProfile>(page, "GET", path);
    const created = await get();
    expect([
        created.name,
        created.version,
        created.payment_instructions,
        created.logo_file_id,
        created.logo_file_version,
    ]).toEqual([name, 1, notes, file.file_id, 1]);
    await button("Back to profile list").click();
    const title = "Fictional renamed company logo";
    const renamed = await api<StoredFile>(page, "PATCH", ledger + "/files/" + file.file_id, {
        expected_version: 1,
        title,
    });
    expect([renamed.id, renamed.version, renamed.title]).toEqual([file.file_id, 2, title]);
    await button(name).click();
    await captureProfile("invoice-profile-retained", "Fictional 公司标志.png · v1");
    await input.fill(editedName + "末");
    await expect(input).toHaveValue(editedName + "末");
    await expect(button("Save profile")).toBeDisabled();
    expect(sent).toHaveLength(2);
    await input.fill(editedName);
    await page
        .getByRole("textbox", { name: "Footer", exact: true })
        .fill("Fictional original footer");
    await button("Save profile").click();
    await expect(page.getByText("Profile verified and saved.")).toBeVisible();
    const updated = await get();
    const retainedLogo = { logo_file_id: file.file_id, logo_file_version: 1 };
    expect(updated).toMatchObject({ version: 2, name: editedName, ...retainedLogo });
    expect(sent.map((s) => JSON.parse(s).name)).toEqual([
        " " + name + " ",
        " " + name + " ",
        editedName,
    ]);
    expect(JSON.parse(sent[2])).toMatchObject(retainedLogo);
    await button("Back to profile list").click();
    await button(editedName).click();
    await button("Archive profile").click();
    await expect(page.getByText("Profile verified and saved.")).toBeVisible();
    expect((await get()).archived).toBe(true);
    let active = await api<InvoiceProfile[]>(
        page,
        "GET",
        ledger + "/invoice-profiles?include_archived=false",
    );
    expect(active.map((r) => r.id)).not.toContain(id);
    await button("Back to profile list").click();
    await button(editedName).click();
    await button("Restore profile").click();
    await expect(page.getByText("Profile verified and saved.")).toBeVisible();
    active = await api<InvoiceProfile[]>(
        page,
        "GET",
        ledger + "/invoice-profiles?include_archived=false",
    );
    expect(active.map((r) => r.id)).toContain(id);
    const out = await api<InvoiceComplete>(page, "POST", ledger + "/invoice-issuances", {
        id: randomUUID(),
        document_id: doc.id,
        expected_document_version: confirmation.document_version,
        configuration_id: id,
        expected_configuration_version: 4,
        previous_issuance_id: null,
    });
    expect(out.number).toBe("演练-001");
    expect(out.artifacts.map((artifact) => artifact.language)).toEqual(["zh", "en"]);
    expect(new Set(out.artifacts.map((artifact) => artifact.file_id)).size).toBe(2);
    for (const f of out.artifacts) {
        const res = await page.request.get(ledger + "/files/" + f.file_id + "/content");
        expect(res.status()).toBe(200);
        expect(
            createHash("sha256")
                .update(await res.body())
                .digest("hex"),
        ).toBe(f.sha256);
    }
});
