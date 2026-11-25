import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { promisify } from "node:util";
import { expect, test } from "@playwright/test";
import { api, expectNoOverflow, fictionalName, login } from "./helpers";
import type { Session } from "../src/api";
import type { Entity } from "../src/ledger-api";
import type { Reminder } from "../src/reminder-api";
import type { DeliveryView, EmailDelivery } from "../src/reminder-email-api";

const due = "2026-01-01";
test("an event keeps its in-app setting and shows the bilingual email schedule and history", async ({
    page,
}) => {
    await page.addInitScript(() => {
        if (!localStorage.getItem("coinpup.locale")) localStorage.setItem("coinpup.locale", "en");
    });
    await login(page);
    const entity = await api<Entity>(page, "POST", "/api/v1/entities", {
        kind: "personal",
        name: fictionalName("reminder mail"),
        base_asset_id: "USD",
        locale: "en",
    });
    const base = `/api/v1/ledgers/${entity.ledger.id}/reminders`;
    const event = await api<Reminder>(page, "POST", base, {
        id: randomUUID(),
        event_kind: "tax",
        title: fictionalName("local reminder"),
        manual_due_date: due,
        manual_reason: "Fictional schedule for local capture",
    });
    const path = `${base}/${event.id}`;
    await api(page, "PUT", path + "/delivery-config", {
        enabled: true,
        lead_days: 0,
        timezone: "UTC",
        minute: 0,
        locale: "en",
        inbox_enabled: true,
        email_enabled: false,
        email_recipient: null,
        expected_event_version: event.version,
        expected_config_version: 0,
    });
    await page.goto(`/?entity=${entity.id}&view=reminders`);
    await page.getByRole("article").filter({ hasText: event.title }).getByRole("button").click();
    const button = (name: string) => page.getByRole("button", { name, exact: true });
    const field = (name: string) => page.getByLabel(name, { exact: true });
    await button("View email reminders").click();
    await field("Enable email reminders").check();
    const recipient = `fictional-${randomUUID().slice(0, 8)}@example.invalid`;
    await field("Email recipient").fill(recipient);
    await field("Email language").selectOption("zh");
    for (const width of [1440, 375, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await button("中文").click();
        await expect(field("接收地址")).toHaveValue(recipient);
        await expect(field("邮件语言")).toHaveValue("zh");
        await expectNoOverflow(page);
        await button("English").click();
        await expect(field("Email recipient")).toHaveValue(recipient);
        await expectNoOverflow(page);
    }
    await button("Save email settings").click();
    const displayed = page.getByRole("region", { name: "Email reminders", exact: true });
    await expect(displayed).toContainText("Email settings saved.");
    await expect(displayed).toContainText(due + "T00:00:00Z");
    await expect(displayed).toContainText("No email deliveries yet.");
    const stored = await api<DeliveryView>(page, "GET", path + "/delivery-config");
    expect(stored.config).toMatchObject({
        inbox_enabled: true,
        email_enabled: true,
        email_recipient: recipient,
        locale: "zh",
    });

    // The disposable local SMTP preview supplies a scoped fixture; CI has no SMTP worker.
    const dispatch = process.env.E2E_REMINDER_MAIL_DISPATCH;
    if (dispatch) {
        const session = await page.request.get("/api/v1/auth/session");
        expect(session.ok()).toBe(true);
        const { user } = (await session.json()) as Session;
        await promisify(execFile)(dispatch, [String(user.id), entity.ledger.id, event.id]);
        await button("Refresh email history").click();
        await expect(displayed.getByText("Accepted by SMTP", { exact: true })).toHaveCount(1);
        await expect(displayed).toContainText("Attempts 1");
        const rows = await api<EmailDelivery[]>(page, "GET", path + "/email-deliveries");
        expect(rows).toMatchObject([{ state: "accepted", attempt_no: 1 }]);
        await button("中文").click();
        await expect(page.getByText("SMTP 已接受", { exact: true })).toBeVisible();
        await expectNoOverflow(page);
    }
});
