import type { CSSProperties } from "react";
import { maskCardNumber } from "./account-details";
import { BrandMark, isPaymentPlatform } from "./BrandMark";
import { accountInstitution, getInstitution } from "./institutions";
import { institutionLogoUrl } from "./institution-brand-logos";
import type { Account } from "./ledger-api";
import "./icon-badges.css";

const paths = new Map([
    ["bank", "m2.5 8 9.5-5 9.5 5v1H2.5V8Zm2 12h15m-16 2h17M6 12v5m6-5v5m6-5v5"],
    ["cash", "M2 5h18v12H2V5Zm3 15h17V9M6 11h.5m9 0h.5m-3-2a2.7 2.7 0 1 1-4 0 2.7 2.7 0 0 1 4 0Z"],
    [
        "credit_card",
        "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm-1 5h18M6 15h5",
    ],
    [
        "debit_card",
        "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm-1 5h18M8 14v3m-2-1 2-2 2 2m5-2v3m-2-2 2 2 2-2",
    ],
    ["crypto", "m12 2 8 10-8 10-8-10 8-10Zm-8 10 8 4 8-4M12 2v14"],
]);
const wallet = "M3 7h16v13H5a2 2 0 0 1-2-2V7Zm0 0V6a2 2 0 0 1 2-2h12v3m2 5h-5v4h5";

export function AccountIcon({
    kind,
    institution,
    customName,
    compact = false,
}: {
    kind: string;
    institution?: string | null;
    customName?: string | null;
    compact?: boolean;
}) {
    const bank = getInstitution(institution);
    const logo = bank?.logo && institutionLogoUrl(bank.logo);
    const initial = Array.from(customName?.trim() ?? "")[0]?.toUpperCase();
    return (
        <span
            className={`account-mark icon-badge${compact ? " compact" : ""}`}
            data-account-kind={kind}
            data-institution={bank?.id}
            aria-hidden="true"
            style={bank ? ({ "--institution-color": bank.color } as CSSProperties) : undefined}
        >
            {bank ? (
                logo ? (
                    <img className="institution-logo" src={logo} alt="" />
                ) : (
                    <span className="institution-mark-text">{bank.shortName}</span>
                )
            ) : initial ? (
                <span className="institution-mark-text">{initial}</span>
            ) : isPaymentPlatform(kind) ? (
                <BrandMark brand={kind} />
            ) : (
                <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.65"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    focusable="false"
                >
                    <path d={paths.get(kind) ?? wallet} />
                </svg>
            )}
        </span>
    );
}

export type AccountIdentity = Pick<Account, "id" | "kind" | "details" | "name" | "crypto_platform">;

export function institutionForAccount(account: AccountIdentity) {
    return account.kind === "crypto" && account.crypto_platform
        ? getInstitution(account.crypto_platform.institution_id)
        : accountInstitution(account.details, account.name, account.kind);
}

export function AccountIdentityIcon({
    account,
    compact,
}: {
    account: AccountIdentity;
    compact?: boolean;
}) {
    const institution = institutionForAccount(account);
    return (
        <AccountIcon
            kind={account.kind}
            institution={institution?.id}
            customName={
                account.kind === "crypto" ? account.crypto_platform?.custom_name : undefined
            }
            compact={compact}
        />
    );
}

export function AccountName({ account }: { account: AccountIdentity }) {
    const suffix = maskCardNumber(account);
    return (
        <span className="icon-label">
            <AccountIdentityIcon account={account} compact />
            <span>
                {account.name}
                {suffix && <small> · {suffix}</small>}
            </span>
        </span>
    );
}
