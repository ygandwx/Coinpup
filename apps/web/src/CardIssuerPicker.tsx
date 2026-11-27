import { useId, useState } from "react";
import { AccountIcon } from "./AccountIcon";
import type { Locale } from "./i18n";
import { BANK_INSTITUTIONS, getInstitution } from "./institutions";

export function CardIssuerPicker({
    locale,
    institutionId,
    onChange,
}: {
    locale: Locale;
    institutionId: string;
    onChange: (id: string) => void;
}) {
    const id = useId();
    const [search, setSearch] = useState("");
    const query = search.trim().normalize("NFKC").toLowerCase();
    const institution = BANK_INSTITUTIONS.find((bank) => bank.id === institutionId);
    const issuers = BANK_INSTITUTIONS.filter(
        (bank) =>
            bank.id === institutionId ||
            [bank.name.zh, bank.name.en, bank.shortName, bank.swiftPrefix, ...bank.aliases].some(
                (part) => part.normalize("NFKC").toLowerCase().includes(query),
            ),
    );
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);

    return (
        <div className="card-issuer-picker">
            <div className="field">
                <label htmlFor={id}>{t("搜索发卡机构", "Search issuers")}</label>
                <input
                    id={id}
                    autoComplete="off"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                />
            </div>
            <fieldset className="card-issuer-options">
                <legend>{t("常用发卡机构", "Common issuers")}</legend>
                <div className="card-issuer-list">
                    <label>
                        <input
                            aria-label={t("手动输入发卡行", "Enter issuer manually")}
                            type="radio"
                            name={id}
                            checked={!institutionId}
                            onChange={() => onChange("")}
                        />
                        {t("手动输入发卡行", "Enter issuer manually")}
                    </label>
                    {institutionId && !institution && (
                        <label>
                            <input type="radio" name={id} checked readOnly />
                            {getInstitution(institutionId)?.name[locale] ?? institutionId}
                        </label>
                    )}
                    {issuers.map((bank) => (
                        <label key={bank.id}>
                            <input
                                type="radio"
                                name={id}
                                checked={bank.id === institutionId}
                                onChange={() => onChange(bank.id)}
                            />
                            <AccountIcon kind="bank" institution={bank.id} compact />
                            <span>{bank.name[locale]}</span>
                        </label>
                    ))}
                </div>
                {!issuers.length && (
                    <p className="help-text">{t("未找到发卡机构。", "No matching issuers.")}</p>
                )}
            </fieldset>
        </div>
    );
}
