import type { ReactNode } from "react";
import { AccountDrawer } from "./AccountDrawer";
import { AccountIdentityIcon } from "./AccountIcon";
import { ACCOUNT_DETAILS, maskCardNumber } from "./account-details";
import type { AccountDetailKey } from "./account-details";
import type { Locale } from "./i18n";
import type { Account } from "./ledger-api";
import { StoredCardNumber } from "./StoredCardNumber";

export function CardDetailsDrawer({
    account,
    locale,
    languageControl,
    onClose,
    onReveal,
    onUnauthorized,
}: {
    account: Account;
    locale: Locale;
    languageControl?: ReactNode;
    onClose: () => void;
    onReveal: (signal: AbortSignal) => Promise<string | null>;
    onUnauthorized: () => void;
}) {
    const mask = maskCardNumber(account);
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const details: AccountDetailKey[] =
        account.kind === "credit_card"
            ? ["statement_day", "payment_due_day", "credit_limit"]
            : ["linked_account_number"];
    return (
        <AccountDrawer
            title={t("卡片详情", "Card details")}
            busy={false}
            onCancel={onClose}
            languageControl={languageControl}
        >
            <div className="account-drawer-fields">
                <div className="card-details-heading">
                    <AccountIdentityIcon account={account} />
                    <h3>{account.name}</h3>
                </div>
                {mask &&
                    (account.has_card_number ? (
                        <StoredCardNumber
                            masked={mask}
                            locale={locale}
                            onReveal={onReveal}
                            onUnauthorized={onUnauthorized}
                        />
                    ) : (
                        <p className="account-card-number">{mask}</p>
                    ))}
                <dl className="profile-details">
                    {(["issuer_name", "expiry", "cardholder", ...details, "note"] as const)
                        .filter((key) => account.details[key])
                        .map((key) => (
                            <div key={key}>
                                <dt>{ACCOUNT_DETAILS[key].label[locale]}</dt>
                                <dd>{account.details[key]}</dd>
                            </div>
                        ))}
                </dl>
            </div>
            <div className="form-actions">
                <button type="button" className="secondary-button" onClick={onClose}>
                    {t("关闭", "Close")}
                </button>
            </div>
        </AccountDrawer>
    );
}
