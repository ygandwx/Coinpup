import { DisplayAmount } from "./DisplayAmount";
import { isCardKind, maskCardNumber } from "./account-details";
import type { CSSProperties } from "react";
import type { Locale } from "./i18n";
import type { Account, Asset } from "./ledger-api";
import { AccountIdentityIcon, institutionForAccount } from "./AccountIcon";
import { AssetIcon } from "./AssetIcon";
import { assetNetworkName } from "./asset-networks";
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
    const platform = account.kind === "crypto" ? account.crypto_platform : null;
    const accountType = platform
        ? account.custody_mode === "custodial"
            ? t("托管平台", "Custodial platform")
            : t("自托管钱包", "Self-custody wallet")
        : t(zh, en);
    const institution = institutionForAccount(account);
    const institutionName = institution?.name[locale] ?? platform?.custom_name;
    const profiles = new Map(
        account.crypto_assets?.map(({ asset_id, details }) => [asset_id, details]),
    );
    const card = isCardKind(account.kind);
    const suffix = maskCardNumber(account);
    const balances = rows.length ? (
        <dl className="funding-balances">
            {rows.map(({ balance, chart }) => {
                const asset = assets.get(balance.asset_id);
                const profile = platform ? profiles.get(balance.asset_id) : undefined;
                const network = asset?.network ?? profile?.network;
                return (
                    <div key={balance.asset_id}>
                        <dt className="icon-label">
                            {asset && (
                                <AssetIcon
                                    asset={asset}
                                    compact
                                    networkBadge={platform ? network : undefined}
                                />
                            )}
                            {platform && asset ? asset.code : balance.asset_id}
                            {platform && network && (
                                <small> · {assetNetworkName(network, locale)}</small>
                            )}
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
                            {profile && (profile.address || profile.memo || profile.label) && (
                                <dl className="funding-coin-profile">
                                    {profile.address && (
                                        <div>
                                            <dt>{t("充值地址", "Deposit address")}</dt>
                                            <dd>{profile.address}</dd>
                                        </div>
                                    )}
                                    {profile.memo && (
                                        <div>
                                            <dt>Memo / Tag</dt>
                                            <dd>{profile.memo}</dd>
                                        </div>
                                    )}
                                    {profile.label && (
                                        <div>
                                            <dt>{t("标签", "Label")}</dt>
                                            <dd>{profile.label}</dd>
                                        </div>
                                    )}
                                </dl>
                            )}
                        </dd>
                    </div>
                );
            })}
        </dl>
    ) : (
        <p className="funding-empty">{t("暂无原币余额", "No asset balances available")}</p>
    );
    return (
        <article
            className="funding-card"
            data-testid={`account-${account.id}`}
            data-recently-saved={recentlySaved || undefined}
        >
            <header className="funding-card-heading">
                <AccountIdentityIcon account={account} />
                <div>
                    <h3>{account.name}</h3>
                    <span>{accountType}</span>
                    {institutionName && institutionName !== accountType && (
                        <span> · {institutionName}</span>
                    )}
                    {suffix && <span className="account-card-number"> · {suffix}</span>}
                </div>
                {account.archived && (
                    <small className="record-badge">{t("已归档", "Archived")}</small>
                )}
            </header>
            {platform ? (
                <details className="funding-coins">
                    <summary>{t("币种和余额", "Coins and balances")}</summary>
                    {balances}
                </details>
            ) : (
                balances
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
