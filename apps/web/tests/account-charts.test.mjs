import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
registerHooks({
    resolve(specifier, context, next) {
        return next(specifier === "./money" ? "./money.ts" : specifier, context);
    },
});
const { prepareAccountBalances } = await import("../src/account-charts.ts");
const account = (id, kind) => ({ id, kind });
const row = (id, asset, amount) => ({ account_id: id, asset_id: asset, amount });

test("native-unit shares use the complete asset ID, same sign and only visible accounts", () => {
    const networkA = "USDC:chain-a:token";
    const networkB = "USDC:chain-b:token";
    const rows = [
        row("bank", networkA, "70.000001"),
        row("cash", networkA, "30.000000"),
        row("processor", networkA, "-30.000000"),
        row("hidden", networkA, "1000000.000000"),
        row("wallet", networkB, "21.000000"),
    ];
    const result = prepareAccountBalances(
        [
            account("bank", "bank"),
            account("cash", "cash"),
            account("processor", "stripe"),
            account("wallet", "crypto"),
        ],
        rows,
    );
    assert.equal(result.get("bank")[0].chart.percentage, "70%");
    assert.equal(result.get("cash")[0].chart.percentage, "30%");
    assert.equal(result.get("processor")[0].chart.kind, "payment");
    assert.equal(result.get("processor")[0].chart.sign, "negative");
    assert.equal(result.get("processor")[0].chart.percentage, "100%");
    assert.equal(result.get("wallet")[0].chart.kind, "holding");
    assert.equal(result.get("wallet")[0].chart.percentage, "100%");
    assert.equal(result.has("hidden"), false);
});

test("card debt and overpayments keep their own denominator; zero is settled", () => {
    const result = prepareAccountBalances(
        ["card-a", "card-b", "card-c", "card-d"]
            .map((id) => account(id, "credit_card"))
            .concat(account("bank", "bank")),
        [
            row("card-a", "USD", "-75.00"),
            row("card-b", "USD", "-25.00"),
            row("card-c", "USD", "5.00"),
            row("card-d", "USD", "0.00"),
            row("bank", "USD", "-200.00"),
        ],
    );
    assert.equal(result.get("card-a")[0].chart.percentage, "75%");
    assert.equal(result.get("card-b")[0].chart.percentage, "25%");
    assert.equal(result.get("card-c")[0].chart.percentage, "100%");
    assert.equal(result.get("card-d")[0].chart.sign, "zero");
    assert.equal(result.get("card-d")[0].chart.width, "0%");
    assert.equal(result.get("bank")[0].chart.percentage, "66.7%");
});

test("18-decimal dust, near-total shares and disabled historical links survive intact", () => {
    const large = row("bank", "ETH", "99999999999999999999.999999999999999999");
    const dust = { ...row("wallet", "ETH", "0.000000000000000001"), link_enabled: false };
    const result = prepareAccountBalances(
        [account("wallet", "crypto"), account("bank", "bank")],
        [dust, large],
    );
    assert.equal(result.get("bank")[0].chart.percentage, ">99.9%");
    assert.equal(result.get("wallet")[0].chart.percentage, "<0.1%");
    assert.equal(result.get("wallet")[0].balance, dust);
});

test("every supported payment platform has a payment chart; unknown kinds retain a balance chart", () => {
    for (const kind of ["wechat", "alipay", "paypal", "wise", "stripe", "legacy-account"]) {
        const result = prepareAccountBalances([account("id", kind)], [row("id", "USD", "0.00")]);
        assert.equal(
            result.get("id")[0].chart.kind,
            kind === "legacy-account" ? "balance" : "payment",
        );
    }
});
