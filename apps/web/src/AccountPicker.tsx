import { useId, useRef, useState } from "react";

import {
    AccountIcon,
    AccountIdentityIcon,
    AccountName,
    institutionForAccount,
} from "./AccountIcon";
import type { AccountIdentity } from "./AccountIcon";
import { maskCardNumber } from "./account-details";
import type { Locale } from "./i18n";
import "./account-picker.css";

type Props = {
    id: string;
    locale: Locale;
    accounts: readonly AccountIdentity[];
    placeholder: string;
    value?: string;
    defaultValue?: string;
    name?: string;
    disabled?: boolean;
    retainedLabel?: string;
    describedBy?: string;
    onChange?: (value: string) => void;
};

/** Keep the actual select for browser form submission, validation and native keyboard controls. */
export function AccountPicker({
    id,
    locale,
    accounts,
    placeholder,
    value,
    defaultValue = "",
    name,
    disabled,
    retainedLabel,
    describedBy,
    onChange,
}: Props) {
    const searchId = useId();
    const select = useRef<HTMLSelectElement>(null);
    const filter = useRef<HTMLDetailsElement>(null);
    const [uncontrolled, setUncontrolled] = useState(defaultValue);
    const [query, setQuery] = useState("");
    const selected = value ?? uncontrolled;
    const current = accounts.find((account) => account.id === selected);
    const word = disabled ? "" : query.trim().normalize("NFKC").toLowerCase();
    const displayed = accounts.filter((account) => {
        if (!word || account.id === selected) return true;
        const institution = institutionForAccount(account);
        return [
            account.name,
            account.kind === "crypto" ? account.crypto_platform?.custom_name : null,
            institution?.name.zh,
            institution?.name.en,
            institution?.shortName,
            ...(institution?.aliases ?? []),
            maskCardNumber(account),
        ].some((label) => label?.normalize("NFKC").toLowerCase().includes(word));
    });
    const search = locale === "zh" ? "搜索账户" : "Search accounts";
    function choose(id: string) {
        if (value === undefined) setUncontrolled(id);
        setQuery("");
        onChange?.(id);
    }
    return (
        <div className="account-picker">
            <div className="account-picker-select">
                <span className="account-picker-selected" aria-hidden="true">
                    {current ? (
                        <AccountIdentityIcon account={current} compact />
                    ) : (
                        <AccountIcon kind="generic" compact />
                    )}
                </span>
                <select
                    id={id}
                    ref={select}
                    name={name}
                    value={selected}
                    required
                    disabled={disabled}
                    aria-describedby={describedBy}
                    onChange={(event) => choose(event.currentTarget.value)}
                >
                    <option value="">{placeholder}</option>
                    {selected && !current && (
                        <option value={selected} disabled>
                            {retainedLabel ?? selected}
                        </option>
                    )}
                    {displayed.map((account) => {
                        const suffix = maskCardNumber(account);
                        return (
                            <option
                                key={account.id}
                                value={account.id}
                                label={account.name}
                                aria-label={account.name}
                                aria-description={suffix}
                            >
                                <AccountIdentityIcon account={account} compact />
                                <span>
                                    {account.name}
                                    {suffix && <small> · {suffix}</small>}
                                </span>
                            </option>
                        );
                    })}
                </select>
            </div>
            {!disabled && accounts.length > 1 && (
                <details
                    ref={filter}
                    className="account-picker-filter"
                    onToggle={(event) => {
                        if (!event.currentTarget.open) setQuery("");
                    }}
                >
                    <summary>{search}</summary>
                    <label htmlFor={searchId} className="visually-hidden">
                        {search}
                    </label>
                    <input
                        id={searchId}
                        type="search"
                        value={query}
                        onChange={(event) => setQuery(event.currentTarget.value)}
                    />
                    {!!word && (
                        <ul className="account-picker-results">
                            {displayed
                                .filter((account) => account.id !== selected)
                                .map((account) => (
                                    <li key={account.id}>
                                        <button
                                            type="button"
                                            onClick={() => {
                                                choose(account.id);
                                                if (filter.current) filter.current.open = false;
                                                select.current?.focus();
                                            }}
                                        >
                                            <AccountName account={account} />
                                        </button>
                                    </li>
                                ))}
                        </ul>
                    )}
                    {word && displayed.every((account) => account.id === selected) && (
                        <p role="status" className="help-text">
                            {locale === "zh" ? "没有其他匹配账户。" : "No other accounts match."}
                        </p>
                    )}
                </details>
            )}
        </div>
    );
}
