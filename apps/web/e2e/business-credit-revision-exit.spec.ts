import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { api } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";
import type { CreditCurrent } from "../src/credit-api";

test("fictional rejected revision can leave a stale editor without rebasing its intent", async ({
    page,
}) => {
    const data = await confirmationSetup(page, "bill");
    await api(page, "POST", `${data.base}/confirm`, { id: randomUUID(), expected_version: 1 });
    const replacement = {
        credit_date: "2026-01-05",
        reason: "Fictional immutable credit",
        lines: [
            {
                source_line_id: data.source.lines[0].id,
                net_amount: "17",
                tax_amount: "2",
                recognition_date: "2026-01-05",
            },
        ],
    };
    const creditId = randomUUID(),
        endpoint = `${data.ledger}/business-credits/${creditId}`;
    await api(page, "POST", `${data.ledger}/business-credits`, {
        id: creditId,
        document_id: data.source.id,
        expected_version: 2,
        ...replacement,
    });
    await page.goto(`/?entity=${data.entity.id}&view=business-credits`);
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    await button("Credit history").click();
    await button("View credit details").click();
    await button("Current state and credit revisions").click();
    await button("Correct complete credit group").click();
    const reason = page.getByLabel("Reason for this correction or cancellation", { exact: true });
    await reason.fill("Fictional preserved rejected input");
    await page
        .getByRole("checkbox", { name: "I reviewed the complete previous group", exact: false })
        .check();
    const advanced = await api(page, "POST", `${endpoint}/corrections`, {
        expected_version: 1,
        reason: "Fictional other operator revision",
        replacement,
    });
    const rejected = page.waitForResponse(
        (response) =>
            response.url().endsWith("/corrections") && response.request().method() === "POST",
    );
    await button("Confirm correction").click();
    expect((await rejected).status()).toBe(409);
    await button("Edit rejected request").click();
    await expect(reason).toHaveValue("Fictional preserved rejected input");
    await expect(page.getByRole("alert")).toContainText("The credit group version changed.");
    await expect(button("Confirm correction")).toBeDisabled();
    const discard = button("Discard edits and return to current state");
    await expect(discard).toBeEnabled();
    await discard.click();
    await expect(page.getByText("Group version: 2", { exact: false })).toBeVisible();
    await button("Correct complete credit group").click();
    await expect(reason).toHaveValue("");
    await expect(reason).toBeEnabled();
    const current = await api<CreditCurrent>(page, "GET", `${endpoint}/current`);
    expect(current.version).toBe(2);
    expect(await api(page, "GET", `${endpoint}/revisions/${current.revision_id}`)).toEqual(
        advanced,
    );
});
