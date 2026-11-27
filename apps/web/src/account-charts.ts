import type { Account, Balance } from "./ledger-api";
import { parseAmount } from "./money";

type ChartKind = "balance" | "holding" | "credit" | "payment";

export type AccountChart = {
    kind: ChartKind;
    asset: string;
    sign: "positive" | "negative" | "zero";
    width: string;
    percentage: string;
};

export type VisualBalance = { balance: Balance; chart: AccountChart };

function chartKind(kind: string): ChartKind {
    switch (kind) {
        case "credit_card":
            return "credit";
        case "crypto":
            return "holding";
        case "wechat":
        case "alipay":
        case "paypal":
        case "wise":
        case "stripe":
            return "payment";
        default:
            return "balance";
    }
}

function percentageOf(amount: bigint, total: bigint) {
    if (amount === 0n) return { width: "0%", percentage: "0%" };
    const thousandths = (amount * 1000n + total / 2n) / total;
    const decimal = thousandths % 10n;
    const width = `${thousandths / 10n}${decimal === 0n ? "" : `.${decimal}`}%`;
    // Rounding a nonzero amount or a partial share must never imply none or all.
    const percentage =
        amount * 1000n < total
            ? "<0.1%"
            : amount < total && amount * 1000n > total * 999n
              ? ">99.9%"
              : width;
    return { width, percentage };
}

/** Accounts and balances come from the selected ledger. Only displayed accounts participate. */
export function prepareAccountBalances(
    accounts: readonly Account[],
    balances: readonly Balance[],
): Map<string, VisualBalance[]> {
    const kinds = new Map(accounts.map((account) => [account.id, chartKind(account.kind)]));
    const totals = new Map<string, bigint>();
    const cardTotals = new Map<string, bigint>();
    const amounts = [];
    for (const balance of balances) {
        const kind = kinds.get(balance.account_id);
        if (!kind) continue;
        // A common integer scale also compares equivalent decimal strings exactly.
        const units = parseAmount(balance.amount, 18);
        const sign = units < 0n ? "negative" : units > 0n ? "positive" : "zero";
        const magnitude = units < 0n ? -units : units;
        const key = `${sign}:${balance.asset_id}`;
        totals.set(key, (totals.get(key) ?? 0n) + magnitude);
        if (kind === "credit") cardTotals.set(key, (cardTotals.get(key) ?? 0n) + magnitude);
        amounts.push({ balance, kind, sign, magnitude, key } as const);
    }
    const byAccount = new Map<string, VisualBalance[]>();
    for (const { balance, kind, sign, magnitude, key } of amounts) {
        const total = (kind === "credit" ? cardTotals : totals).get(key)!;
        const rows = byAccount.get(balance.account_id) ?? [];
        rows.push({
            balance,
            chart: { kind, asset: balance.asset_id, sign, ...percentageOf(magnitude, total) },
        });
        byAccount.set(balance.account_id, rows);
    }
    return byAccount;
}
