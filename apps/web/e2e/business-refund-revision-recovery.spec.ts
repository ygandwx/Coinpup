import { expect, test } from "@playwright/test";
import { api, username } from "./helpers";
import { openRefundVersions, refundRevisionFixture } from "./refund-revision-helpers";
import { parseAmount } from "../src/money";
import type { Balance } from "../src/ledger-api";
import type {
    RefundCurrent,
    RefundHistoryEntry,
    RefundDetail,
    RefundRevisionReceipt,
} from "../src/refund-api";

test("real sign-in after a lost cancellation only replays the original key, raw body and refund", async ({
    page,
}) => {
    const f = await refundRevisionFixture(page),
        versions = await openRefundVersions(page, f.entity.id, "en");
    const bank = async () => {
        const rows = await api<Balance[]>(page, "GET", f.ledger + "/balances?limit=100");
        const usd = rows.filter((row) => row.account_id === f.bank.id && row.asset_id === "USD");
        expect(usd).toHaveLength(1);
        return parseAmount(usd[0].amount, 2);
    };
    expect(await bank()).toBe(8000n);
    await versions.getByRole("button", { name: "Cancel this refund", exact: true }).click();
    await versions
        .getByLabel("Cancellation reason", { exact: true })
        .fill("Fictional lost cancellation");
    await versions.getByRole("checkbox", { name: /I reviewed this refund/ }).check();
    let firstReceipt: Buffer | undefined;
    const cancellationResponse = () =>
        page.waitForResponse(
            (r) =>
                r.request().method() === "POST" &&
                new URL(r.url()).pathname === f.refundPath + "/cancellations",
        );
    const collected: { body: string; key: string; path: string }[] = [];
    await page.route("**" + f.refundPath + "/cancellations", async (route) => {
        const request = route.request();
        collected.push({
            body: request.postData()!,
            key: request.headers()["idempotency-key"],
            path: request.url(),
        });
        if (collected.length === 1) {
            const posted = await route.fetch();
            expect(posted.status()).toBe(200);
            firstReceipt = await posted.body();
            return route.abort("failed");
        }
        if (collected.length === 2) {
            const session = await (await page.request.get("/api/v1/auth/session")).json();
            const logout = await page.request.post("/api/v1/auth/logout", {
                headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": session.csrf_token },
            });
            expect(logout.ok()).toBe(true);
        }
        return route.continue();
    });
    await versions
        .getByRole("button", { name: "Confirm refund cancellation", exact: true })
        .click();
    const retry = page.getByRole("button", { name: "Retry original request", exact: true });
    await expect(retry).toBeVisible();
    expect(await bank()).toBe(10000n);
    const denied = cancellationResponse();
    await retry.click();
    expect((await denied).status()).toBe(401);
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD!);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    const replay = cancellationResponse();
    await retry.click();
    const result = await replay;
    expect(result.status()).toBe(200);
    expect(firstReceipt).toBeDefined();
    expect(await result.body()).toEqual(firstReceipt);
    const receipt = (await result.json()) as RefundRevisionReceipt;
    expect(receipt.action).toBe("cancel");
    expect(receipt.version).toBe(2);
    expect(await bank()).toBe(10000n);
    expect((await api<RefundDetail>(page, "GET", f.refundPath)).receipt).toEqual(f.original);
    expect(await api(page, "GET", f.refundPath + "/revisions/" + receipt.revision_id)).toEqual(
        receipt,
    );
    await expect(page.getByText(/Verified saved receipt, version 2/)).toBeVisible();
    expect(collected).toHaveLength(3);
    expect(collected[0].key).toBeTruthy();
    expect(collected[0]).toEqual(collected[1]);
    expect(collected[1]).toEqual(collected[2]);
    const entries = await api<RefundHistoryEntry[]>(page, "GET", f.refundPath + "/history");
    expect(entries.map((r) => r.action)).toEqual(["create", "cancel"]);
    expect((await api<RefundCurrent>(page, "GET", f.refundPath + "/current")).fact).toBeNull();
});
