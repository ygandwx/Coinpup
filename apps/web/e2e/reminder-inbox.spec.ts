import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { promisify } from "node:util";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login } from "./helpers";
import type { Entity } from "../src/ledger-api";
import type { Reminder } from "../src/reminder-api";
import type { DeliveryView } from "../src/reminder-email-api";
import type { InboxDelivery } from "../src/reminder-inbox-api";
import type { Session } from "../src/api";

test("an in-app reminder keeps its date and separates reading from completion and email", async ({
    page,
}) => {
    await page.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("inbox entity"),
        base_asset_id: "USD",
        locale: "en",
    });
    const base = `/api/v1/ledgers/${entity.ledger.id}/reminders`;
    const event = await api<Reminder>(page, "POST", base, {
        id: randomUUID(),
        event_kind: "certificate",
        title: fictionalName("certificate reminder"),
        manual_due_date: "2026-02-03",
        manual_reason: "Fictional certificate date",
    });
    const eventPath = base + "/" + event.id;
    await page.goto(`/?entity=${entity.id}&view=reminders`);
    await page.getByRole("article").filter({ hasText: event.title }).getByRole("button").click();
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    const field = (name: string) => page.getByLabel(name, { exact: true });
    await button("View delivery settings").click();
    await field("Enable inbox reminders").check();
    await field("Days before due").fill("7");
    await field("Local time").fill("00:00");
    await button("中文").click();
    await expect(field("启用站内提醒")).toBeChecked();
    await expect(field("启用邮件提醒")).not.toBeChecked();
    await expect(field("到期前天数")).toHaveValue("7");
    await page.setViewportSize({ width: 320, height: 1000 });
    await expectNoOverflow(page);
    await button("English").click();
    await button("Save delivery settings").click();
    await expect(page.getByText("Delivery settings saved.", { exact: true })).toBeVisible();
    const config = await api<DeliveryView>(page, "GET", eventPath + "/delivery-config");
    expect(config.config).toMatchObject({ inbox_enabled: true, email_enabled: false });
    expect(config.config?.saved_plan?.scheduled_at).toBe("2026-01-27T00:00:00Z");
    await button("Back to events").click();
    await button("View inbox").click();
    await expect(page.getByText("No inbox notices on this page.", { exact: true })).toBeVisible();

    // The disposable test runner invokes the already scoped job when supplied.
    const dispatch = process.env.E2E_REMINDER_INBOX_DISPATCH;
    if (dispatch) {
        const response = await page.request.get("/api/v1/auth/session");
        expect(response.ok()).toBe(true);
        const { user } = (await response.json()) as Session;
        await promisify(execFile)(dispatch, [String(user.id), entity.ledger.id]);
        await button("Refresh inbox").click();
        const notice = page.getByTestId("reminder-inbox-delivery");
        await expect(notice).toHaveCount(1);
        await expect(notice).toContainText(event.title);
        await expect(notice).toContainText("Certificate expiry");
        await expect(notice).toContainText("2026-02-03");
        await button("Mark as read").click();
        await expect(notice).toContainText("Read at");
        const entries = await api<InboxDelivery[]>(page, "GET", base + "/inbox");
        expect(entries).toMatchObject([{ event_id: event.id, version: 2 }]);
        expect((await api<Reminder>(page, "GET", eventPath)).completed).toBe(false);
        expect(await api(page, "GET", eventPath + "/email-deliveries")).toEqual([]);
        await button("中文").click();
        await expect(notice).toContainText("已读时间");
        await expectNoOverflow(page);
        await field("仅未读").check();
        await expect(notice).toHaveCount(0);
    }
});
