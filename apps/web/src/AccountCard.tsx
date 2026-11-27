import type { CSSProperties } from "react";
import type { Locale } from "./i18n";
import type { Account, AccountKind } from "./ledger-api";
import type { AccountChart, VisualBalance } from "./account-charts";
import { formatAmount } from "./money";
import "./account-card.css";

const wallet = "M3 7h16v13H5a2 2 0 0 1-2-2V7Zm0 0V6a2 2 0 0 1 2-2h12v3m2 5h-5v4h5";
const identities = new Map<AccountKind, readonly [string, string, string]>([
    [
        "bank",
        [
            "银行账户",
            "Bank account",
            "m2.5 8 9.5-5 9.5 5v1H2.5V8Zm2 12h15m-16 2h17M6 12v5m6-5v5m6-5v5",
        ],
    ],
    [
        "cash",
        [
            "现金",
            "Cash",
            "M2 5h18v12H2V5Zm3 15h17V9M6 11h.5m9 0h.5m-3-2a2.7 2.7 0 1 1-4 0 2.7 2.7 0 0 1 4 0Z",
        ],
    ],
    [
        "credit_card",
        [
            "信用卡",
            "Credit card",
            "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm-1 5h18M6 15h5",
        ],
    ],
    [
        "wechat",
        [
            "微信",
            "WeChat",
            "M12 14a8 8 0 0 1-5 0l-3 2 1-4a5 5 0 0 1-2-4c0-3 3-5 7-5 3 0 6 2 6 4M21 18l1 3-4-2c-4 1-7-1-7-4 0-3 2-5 6-5s6 2 6 5c0 1-1 2-2 3Z",
        ],
    ],
    ["alipay", ["支付宝", "Alipay", "M16 3H5v18h12v-4M8 6h5m-4 12h3m1-8 3 3 5-5"]],
    [
        "paypal",
        ["PayPal", "PayPal", "M5 19 8 3h6c5 0 6 4 3 7-1 2-4 3-8 3m4-6h2m4 1c4 5-1 9-7 9l-1 4H8"],
    ],
    ["wise", ["Wise", "Wise", "M3 8h16m-5-5 5 5-5 5M21 16H5m5-5-5 5 5 5"]],
    [
        "stripe",
        [
            "Stripe",
            "Stripe",
            "M7 2h10a1 1 0 0 1 1 1v18H6V3a1 1 0 0 1 1-1Zm2 4h6v4H9V6Zm0 8h1m3 0h1m-5 3h1m3 0h1",
        ],
    ],
    [
        "crypto",
        ["加密资产钱包", "Crypto wallet", "m12 2 8 10-8 10-8-10 8-10Zm-8 10 8 4 8-4M12 2v14"],
    ],
]);

// The read API accepts legacy kinds as strings. Unknown kinds retain their original label.
function identity(kind: string) {
    return identities.get(kind as AccountKind) ?? [kind, kind, wallet];
}

export function AccountMark({ kind, compact = false }: { kind: string; compact?: boolean }) {
    return (
        <span
            className={`account-mark${compact ? " compact" : ""}`}
            data-account-kind={kind}
            aria-hidden="true"
        >
            <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.65"
                strokeLinecap="round"
                strokeLinejoin="round"
            >
                <path d={identity(kind)[2]} />
            </svg>
        </span>
    );
}

export function AccountBalanceChart({ chart, locale }: { chart: AccountChart; locale: Locale }) {
    const { kind, sign, percentage, width, asset } = chart;
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const credit = kind === "credit";
    let label: string;
    if (sign === "zero") {
        label = credit ? t("已结清", "Settled") : t("零余额", "Zero balance");
    } else if (credit) {
        label =
            sign === "negative"
                ? t("卡片欠款占比", "Share of card debt")
                : t("卡片溢缴款占比", "Share of card credit");
    } else {
        label =
            sign === "negative"
                ? t("负余额占比", "Negative balance share")
                : t("正余额占比", "Positive balance share");
    }
    const context = credit
        ? t("已显示信用卡", "visible credit cards")
        : t("已显示账户", "visible accounts");
    const description =
        sign === "zero"
            ? `${asset} · ${label}`
            : `${asset} · ${label} · ${percentage} (${context})`;
    return (
        <span
            className="account-chart"
            data-chart={kind}
            data-sign={sign}
            role="img"
            aria-label={description}
            title={description}
            style={{ "--share": width } as CSSProperties}
        >
            <span className="chart-track" aria-hidden="true">
                <i />
            </span>
            <span className="chart-caption" aria-hidden="true">
                {label}
                {sign !== "zero" && (
                    <>
                        {" "}
                        <b>{percentage}</b>
                    </>
                )}
            </span>
        </span>
    );
}

type Props = {
    account: Account;
    rows: VisualBalance[];
    locale: Locale;
    disabled: boolean;
    onEdit: () => void;
    onArchive: () => void;
};

export function AccountCard({ account, rows, locale, disabled, onEdit, onArchive }: Props) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [zh, en] = identity(account.kind);
    return (
        <article className="funding-card" data-testid={`account-${account.id}`}>
            <header className="funding-card-heading">
                <AccountMark kind={account.kind} />
                <div>
                    <h3>{account.name}</h3>
                    <span>{t(zh, en)}</span>
                </div>
                {account.archived && (
                    <small className="record-badge">{t("已归档", "Archived")}</small>
                )}
            </header>
            {rows.length ? (
                <dl className="funding-balances">
                    {rows.map(({ balance, chart }) => (
                        <div key={balance.asset_id}>
                            <dt>
                                {balance.asset_id}
                                {(!balance.link_enabled || !balance.asset_enabled) && (
                                    <small>{t(" · 已停用", " · Disabled")}</small>
                                )}
                            </dt>
                            <dd>
                                <span
                                    className="funding-amount"
                                    tabIndex={0}
                                    aria-label={`${balance.asset_id} ${balance.amount}`}
                                >
                                    {formatAmount(balance.amount, locale)}
                                </span>
                                <AccountBalanceChart chart={chart} locale={locale} />
                            </dd>
                        </div>
                    ))}
                </dl>
            ) : (
                <p className="funding-empty">{t("暂无原币余额", "No asset balances available")}</p>
            )}
            <footer className="row-actions funding-actions">
                <button disabled={disabled} onClick={onEdit}>
                    {t("编辑", "Edit")}
                </button>
                <button disabled={disabled} onClick={onArchive}>
                    {account.archived ? t("恢复", "Restore") : t("归档", "Archive")}
                </button>
            </footer>
        </article>
    );
}
