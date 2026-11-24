import { createHash, randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow } from "./helpers";
import type { InvoiceComplete } from "../src/invoice-issuance-receipt";
import { invoiceSetup } from "./invoice-helpers";

test("company issuance history downloads both original private PDFs, including after locale and size changes", async ({
    page,
}, info) => {
    const { company, ledger, doc, confirmation, profile } = await invoiceSetup(page);
    const receipt = await api<InvoiceComplete>(page, "POST", ledger + "/invoice-issuances", {
        id: randomUUID(),
        document_id: doc.id,
        expected_document_version: confirmation.document_version,
        configuration_id: profile.id,
        expected_configuration_version: profile.version,
        previous_issuance_id: null,
    });
    expect(receipt.number).toBe("示例-0001");
    await page.goto(`/?entity=${company.id}&view=invoices`);
    await page.getByRole("button", { name: "示例-0001", exact: true }).click();
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    const list = page.getByRole("region", { name: /^(Issuance history|签发历史)$/u });
    for (const width of [1440, 375, 320])
        for (const locale of ["zh", "en"]) {
            await page.setViewportSize({ width, height: 950 });
            await button(locale === "zh" ? "中文" : "English").click();
            await expect(list.getByText("示例-0001", { exact: true })).toBeVisible();
            await expect(
                list.getByText(locale === "zh" ? "已签发两份原件" : "Both originals issued", {
                    exact: true,
                }),
            ).toBeVisible();
            await expectNoOverflow(page);
            await page.screenshot({
                path: info.outputPath(`invoice-history-${locale}-${width}.png`),
                fullPage: true,
            });
        }
    for (const [i, name] of ["Download Chinese original", "Download English original"].entries()) {
        const done = page.waitForEvent("download");
        await button(name).press("Enter");
        const got = await done;
        expect(got.suggestedFilename()).toBe(`Invoice-1-${receipt.artifacts[i].language}.pdf`);
        const bytes = await readFile((await got.path())!);
        expect(bytes.subarray(0, 5).toString()).toBe("%PDF-");
        expect(bytes.length).toBe(receipt.artifacts[i].byte_size);
        expect(createHash("sha256").update(bytes).digest("hex")).toBe(receipt.artifacts[i].sha256);
    }
    let unexpected = 0;
    page.on("download", () => unexpected++);
    const file = receipt.artifacts[0];
    await page.route("**/files/" + file.file_id + "/content", async (route) => {
        const actual = await route.fetch();
        const data = Buffer.from(await actual.body());
        data[10] ^= 1;
        await route.fulfill({ response: actual, body: data });
    });
    await button("Download Chinese original").click();
    await expect(list.getByRole("alert")).toContainText("could not be verified");
    expect(unexpected).toBe(0);
    await button("Back to records").click();
    await expect(button("示例-0001")).toBeVisible();
});
