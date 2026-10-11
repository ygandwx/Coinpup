import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { draftFields } from "../src/draft-fields";
import { api, username } from "./helpers";
import { confirmationSetup } from "./business-confirmation-helpers";

test("fictional confirmation from the second draft page returns to the first sealed page", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await confirmationSetup(page);
    let targetId = data.source.id;
    for (let index = 0; index < 25; index += 1) {
        const fields = draftFields(data.source);
        const created = await api<{ id: string }>(
            page,
            "POST",
            `${data.ledger}/business-documents`,
            {
                ...fields,
                id: randomUUID(),
                lines: fields.lines.map((line) => ({ ...line, id: randomUUID() })),
            },
        );
        targetId = created.id;
    }
    await page.getByRole("button", { name: "Refresh list", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Review saved version", exact: true }),
    ).toHaveCount(25);
    await page.getByRole("button", { name: "Next page", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Review saved version", exact: true }),
    ).toHaveCount(1);
    await page.getByRole("button", { name: "Review saved version", exact: true }).click();
    await expect(page.locator(".business-confirmation-panel")).toContainText(targetId);
    await page.getByRole("checkbox", { name: "I reviewed amounts", exact: false }).check();
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    await expect(
        page.getByText("Confirmed; original amounts and journal mappings are sealed.", {
            exact: true,
        }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Back to documents", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "View sealed document", exact: true }),
    ).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Previous page", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Control balances", exact: true }).click();
    await expect(page.getByTestId("control-amount")).toHaveText("110.00 USD");
    expect(await api(page, "GET", `${data.ledger}/balances`)).toEqual([]);
});

test("fictional stale confirmation resolves only after a higher saved version is observed", async ({
    page,
}) => {
    const data = await confirmationSetup(page);
    await page.getByRole("button", { name: "Review saved version", exact: true }).click();
    await page.getByRole("checkbox", { name: "I reviewed amounts", exact: false }).check();
    let posts = 0;
    await page.route(`**${data.base}/confirm`, async (route) => {
        if (++posts === 1) return route.abort("failed");
        await route.continue();
    });
    await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original confirmation", exact: true }),
    ).toBeVisible();
    await api(page, "PATCH", `${data.base}/archive`, { expected_version: 1, archived: true });
    await page.route(`**${data.base}`, (route) =>
        route.request().method() === "GET"
            ? route.fulfill({ status: 503, body: "{}", contentType: "application/json" })
            : route.continue(),
    );
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Retry original confirmation", exact: true }),
    ).toBeEnabled();
    await expect(
        page.getByRole("button", { name: "Back to documents", exact: true }),
    ).toBeDisabled();
    await page.unroute(`**${data.base}`);
    await page.getByRole("button", { name: "Retry original confirmation", exact: true }).click();
    await expect(page.locator(".business-confirmation-panel [role=alert]")).toContainText(
        "This record changed elsewhere",
    );
    await expect(
        page.getByRole("button", { name: "Back to documents", exact: true }),
    ).toBeEnabled();
    await expect(
        page.getByText("Confirmed; original amounts and journal mappings are sealed.", {
            exact: true,
        }),
    ).toHaveCount(0);
    expect(await api(page, "GET", `${data.ledger}/business-confirmations`)).toEqual([]);
    await page.getByRole("button", { name: "Back to documents", exact: true }).click();
    await page.getByRole("button", { name: "Review saved version", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Confirm and post", exact: true }),
    ).toBeDisabled();
});

test("fictional completed confirmation cannot restore private intent after explicit logout", async ({
    page,
}) => {
    test.setTimeout(90_000);
    const data = await confirmationSetup(page);
    await page.getByRole("button", { name: "Review saved version", exact: true }).click();
    await page.getByRole("checkbox", { name: "I reviewed amounts", exact: false }).check();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route(`**${data.base}/confirm`, async (route) => {
        const response = await route.fetch();
        expect(response.status()).toBe(201);
        await gate;
        await route.fulfill({ response });
    });
    try {
        await page.getByRole("button", { name: "Confirm and post", exact: true }).click();
        await expect
            .poll(async () => (await page.request.get(`${data.base}/confirmation`)).status())
            .toBe(200);
        await page.getByRole("button", { name: "Sign out", exact: true }).click();
        await expect(page.locator(".business-confirmation-panel")).toHaveCount(0);
        await page.getByLabel("Username", { exact: true }).fill(username);
        await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
        await page.getByRole("button", { name: "Sign in", exact: true }).click();
        await page.getByRole("button", { name: "Parties and projects", exact: true }).click();
        const delivered = page.waitForResponse((response) =>
            response.url().endsWith(`${data.base}/confirm`),
        );
        release();
        await delivered;
        await expect(
            page.getByRole("heading", { name: "Parties and projects", exact: true }).first(),
        ).toBeVisible();
        await expect(page.locator(".business-confirmation-panel")).toHaveCount(0);
        await page.getByRole("button", { name: "Confirm documents", exact: true }).click();
        await expect(
            page.getByRole("button", { name: "Retry original confirmation", exact: true }),
        ).toHaveCount(0);
        await page.getByRole("button", { name: "Confirmed documents", exact: true }).click();
        await expect(
            page.getByRole("button", { name: "View sealed document", exact: true }),
        ).toHaveCount(1);
    } finally {
        release();
        await page.unroute(`**${data.base}/confirm`);
    }
});
