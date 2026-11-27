import { DisplayAmount } from "./DisplayAmount";
import { isCardKind, maskCardNumber } from "./account-details";
import type { CSSProperties } from "react";
import type { Locale } from "./i18n";
import type { Account, Asset } from "./ledger-api";
import { AccountIcon } from "./AccountIcon";
import { AssetIcon } from "./AssetIcon";
import { accountInstitution } from "./institutions";
import type { AccountChart, VisualBalance } from "./account-charts";
import "./account-card.css";

const identities = new Map([
    ["bank", ["银行账户", "Bank account"]],
    ["cash", ["现金", "Cash"]],
    ["credit_card", ["信用卡", "Credit card"]],
    ["debit_card", ["借记卡", "Debit card"]],
    ["wechat", ["微信", "WeChat"]],
    ["alipay", ["支付宝", "Alipay"]],
    ["paypal", ["PayPal", "PayPal"]],
    ["wise", ["Wise", "Wise"]],
    ["stripe", ["Stripe", "Stripe"]],
    ["crypto", ["加密资产钱包", "Crypto wallet"]],
]);

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
    assets: ReadonlyMap<string, Asset>;
    rows: VisualBalance[];
    locale: Locale;
    disabled: boolean;
    recentlySaved?: boolean;
    onEdit: () => void;
    onDetails: () => void;
    onArchive: () => void;
};

export function AccountCard({
    account,
    assets,
    rows,
    locale,
    disabled,
    recentlySaved,
    onEdit,
    onDetails,
    onArchive,
}: Props) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [zh, en] = identities.get(account.kind) ?? [account.kind, account.kind];
    const accountType = t(zh, en);
    const institution = accountInstitution(account.details, account.name, account.kind);
    const card = isCardKind(account.kind);
    const suffix = maskCardNumber(account);
    return (
        <article
            className="funding-card"
            data-testid={`account-${account.id}`}
            data-recently-saved={recentlySaved || undefined}
        >
            <header className="funding-card-heading">
                <AccountIcon kind={account.kind} institution={institution?.id} />
                <div>
                    <h3>{account.name}</h3>
                    <span>{accountType}</span>
                    {institution && institution.name[locale] !== accountType && (
                        <span> · {institution.name[locale]}</span>
                    )}
                    {suffix && <span className="account-card-number"> · {suffix}</span>}
                </div>
                {account.archived && (
                    <small className="record-badge">{t("已归档", "Archived")}</small>
                )}
            </header>
            {rows.length ? (
                <dl className="funding-balances">
                    {rows.map(({ balance, chart }) => {
                        const asset = assets.get(balance.asset_id);
                        return (
                            <div key={balance.asset_id}>
                                <dt className="icon-label">
                                    {asset && <AssetIcon asset={asset} compact />}
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
                                        <DisplayAmount
                                            amount={balance.amount}
                                            assetId={balance.asset_id}
                                            locale={locale}
                                        />
                                    </span>
                                    <AccountBalanceChart chart={chart} locale={locale} />
                                </dd>
                            </div>
                        );
                    })}
                </dl>
            ) : (
                <p className="funding-empty">{t("暂无原币余额", "No asset balances available")}</p>
            )}
            <footer className="row-actions funding-actions">
                {card && <button onClick={onDetails}>{t("卡片详情", "Card details")}</button>}
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
