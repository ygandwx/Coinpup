import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type {
    Account,
    AccountCreateBody,
    AccountKind,
    AccountUpdateBody,
    Asset,
} from "./ledger-api";
import { AccountIcon } from "./AccountIcon";
import { AssetIcon } from "./AssetIcon";
import { CardIssuerPicker } from "./CardIssuerPicker";
import { accountInstitution, BANK_INSTITUTIONS, getInstitution } from "./institutions";
import {
    ACCOUNT_DETAILS,
    ACCOUNT_DETAIL_KEYS,
    BANK_REGIONS,
    WALLET_NETWORKS,
    WALLET_STORAGE,
    accountFields,
    defaultAssetIds,
    isAccountAssetVisible,
    isCardKind,
    validCardDay,
    validCardExpiry,
    validCardLastFour,
    validCardNumber,
    validCreditLimit,
    validWalletAddress,
} from "./account-details";
import type { AccountDetailKey } from "./account-details";
import { DetailEditor } from "./EntityForm";
import { compileDetails, containsControlCharacters, detailRows, replaceDetail } from "./regions";
import type { LocalizedLabel } from "./regions";
import "./account-editor.css";

const ACCOUNT_KINDS = [
    "bank",
    "cash",
    "wechat",
    "alipay",
    "credit_card",
    "debit_card",
    "paypal",
    "wise",
    "stripe",
    "crypto",
] as const;

const labels = {
    zh: {
        name: "账户名称",
        kind: "账户类型",
        assets: "资产",
        details: "账户资料",
        save: "保存",
        saving: "保存中…",
        cancel: "取消",
        reload: "重新载入",
        assetHelp: "至少选择一种资产。法币和加密资产均按原资产保存，不自动换算。",
        disabled: "已停用",
        missing: "未载入",
        noAssets: "暂无可用资产，请先配置资产目录。",
        showAll: "显示全部资产",
        otherDetails: "其他已保存资料",
        customDetails: "自定义资料",
        searchBank: "搜索银行",
        otherBank: "手动输入银行",
        emptyChoice: "请选择",
        otherNetwork: "其他网络",
        customNetwork: "网络名称",
        publicAddress: "仅填写公开收款地址。",
        cardHelp: "填写新卡号以保存或替换。留空保留已有卡号。",
        cardNumber: "完整卡号",
        debitHelp: "借记卡有独立余额。同一笔资金只建立一个账户，避免重复计入银行账户和借记卡。",
        walletError: "地址格式与所选网络不符。",
        cardError: "请输入 12–19 位有效数字卡号。",
        suffixError: "请输入卡号后四位（仅限四个数字）。",
        expiryError: "请按 MM/YY 格式填写有效期，月份为 01 至 12。",
        limitError: "请填写非负十进制额度，整数最多 20 位，小数最多 18 位。",
        dayError: "账单日和还款日应为 1 至 31。",
        institutionRegion: "银行地区由所选银行决定。",
        nameError: "请输入有效的账户名称，名称不能只包含空格或控制字符。",
        assetsError: "请至少选择一种资产，最多选择 100 种。",
        disabledError:
            "资产选择有变化，请移除已停用或未载入的资产；不改变资产选择时仍可编辑其他资料。",
        detailsError:
            "请检查资料：字段名称须唯一、不能占用预设字段名，名称最长 64 字、内容最长 2000 字，最多 32 项。",
        kinds: {
            bank: "银行账户",
            cash: "现金",
            wechat: "微信",
            alipay: "支付宝",
            credit_card: "信用卡",
            debit_card: "借记卡",
            paypal: "PayPal",
            wise: "Wise",
            stripe: "Stripe",
            crypto: "加密资产钱包",
        },
    },
    en: {
        name: "Account name",
        kind: "Account type",
        assets: "Assets",
        details: "Account details",
        save: "Save",
        saving: "Saving…",
        cancel: "Cancel",
        reload: "Reload",
        assetHelp:
            "Select at least one asset. Fiat and crypto holdings retain their original asset; no conversion is applied.",
        disabled: "Disabled",
        missing: "Not loaded",
        noAssets: "No assets are available. Configure the asset catalog first.",
        showAll: "Show all assets",
        otherDetails: "Other saved details",
        customDetails: "Custom details",
        searchBank: "Search banks",
        otherBank: "Enter bank manually",
        emptyChoice: "Choose an option",
        otherNetwork: "Other network",
        customNetwork: "Network name",
        publicAddress: "Enter only a public receiving address.",
        cardHelp: "Enter a card number to add or replace it. Leave blank to keep the saved number.",
        cardNumber: "Full card number",
        debitHelp:
            "A debit card has its own balance. Keep the same funds in a single account to avoid counting them twice.",
        walletError: "The address does not match the selected network's format.",
        cardError: "Enter a valid card number of 12–19 digits.",
        suffixError: "Enter exactly four digits from the end of the card number.",
        expiryError: "Enter the expiration date as MM/YY, using a month from 01 to 12.",
        limitError: "Enter a nonnegative decimal with up to 20 whole and 18 fractional digits.",
        dayError: "Statement and payment due days must be between 1 and 31.",
        institutionRegion: "The selected bank determines its region.",
        nameError: "Enter an account name without control characters; spaces alone are not a name.",
        assetsError: "Select between 1 and 100 assets.",
        disabledError:
            "Your asset selection changed. Remove disabled or unloaded assets, or keep the original selection to edit other details.",
        detailsError:
            "Check details: use unique, non-reserved names of up to 64 characters, values of up to 2000 characters, and no more than 32 fields.",
        kinds: {
            bank: "Bank account",
            cash: "Cash",
            wechat: "WeChat",
            alipay: "Alipay",
            credit_card: "Credit card",
            debit_card: "Debit card",
            paypal: "PayPal",
            wise: "Wise",
            stripe: "Stripe",
            crypto: "Crypto wallet",
        },
    },
};

export type AccountFormProps = {
    locale: Locale;
    assets: Asset[];
    account?: Account;
    defaultBankRegion?: string | null;
    busy: boolean;
    error?: string | null;
    onSubmit: (body: AccountCreateBody | AccountUpdateBody) => void;
    onCancel: () => void;
    onReload?: () => void;
};

export function AccountForm({
    locale,
    assets,
    account,
    defaultBankRegion,
    busy,
    error,
    onSubmit,
    onCancel,
    onReload,
}: AccountFormProps) {
    const t = labels[locale];
    const prefix = useId();
    const [id] = useState(() => crypto.randomUUID());
    const [expectedVersion] = useState(account?.version);
    const [originalAssetIds] = useState(() => [...(account?.asset_ids ?? [])]);
    const [name, setName] = useState(account?.name ?? "");
    const [kind, setKind] = useState(account?.kind ?? "bank");
    const [assetIds, setAssetIds] = useState<string[]>(() => [...originalAssetIds]);
    const [assetsTouched, setAssetsTouched] = useState(false);
    const [showAll, setShowAll] = useState(false);
    const [rows, setRows] = useState(() => detailRows(account?.details ?? {}, ACCOUNT_DETAIL_KEYS));
    const [bankSearch, setBankSearch] = useState("");
    const [cardNumber, setCardNumber] = useState("");
    const [otherWallet, setOtherWallet] = useState(
        () =>
            !!account?.details.wallet_network &&
            !Object.hasOwn(WALLET_NETWORKS, account.details.wallet_network),
    );
    const [localError, setLocalError] = useState<
        | "name"
        | "assets"
        | "disabled"
        | "details"
        | "card"
        | "suffix"
        | "expiry"
        | "limit"
        | "day"
        | "wallet"
        | null
    >(null);
    const values = new Map(rows.filter((row) => !row.custom).map(({ key, value }) => [key, value]));
    const institution = BANK_INSTITUTIONS.find((bank) => bank.id === values.get("institution"));
    const suggestedInstitution =
        account && kind === account.kind && !values.get("institution")
            ? accountInstitution(account.details, account.name, account.kind)
            : undefined;
    const card = isCardKind(kind);
    const bankRegion = institution?.region ?? values.get("bank_region") ?? defaultBankRegion ?? "";
    const fields = accountFields(kind, bankRegion);
    const hidden = rows.filter(
        (row) => !row.custom && !!row.value && !fields.some((field) => field === row.key),
    );
    const custom = rows.filter((row) => row.custom);
    const query = bankSearch.trim().toLowerCase();
    const banks = BANK_INSTITUTIONS.filter(
        (bank) =>
            bank.id === institution?.id ||
            [bank.name.zh, bank.name.en, bank.shortName, bank.swiftPrefix, ...bank.aliases].some(
                (value) => value.toLowerCase().includes(query),
            ),
    );
    const availableAssets = assets.filter(
        (asset) =>
            (asset.enabled || originalAssetIds.includes(asset.asset_id)) &&
            isAccountAssetVisible(asset, kind, assetIds, showAll),
    );
    const missingAssetIds = (account?.asset_ids ?? []).filter(
        (assetId) => !assets.some((asset) => asset.asset_id === assetId),
    );

    function changeKind(next: AccountKind) {
        setKind(next);
        if (!isCardKind(next)) setCardNumber("");
        if (!account && !assetsTouched) setAssetIds(defaultAssetIds(next, assets));
        setLocalError(null);
    }

    function setField(key: AccountDetailKey, value: string) {
        setRows((prior) => replaceDetail(prior, key, value));
        setLocalError(null);
    }

    function selectBank(id: string) {
        const bank = getInstitution(id);
        setRows((prior) => {
            const next = replaceDetail(prior, "institution", id);
            if (!bank) return next;
            if (card) return replaceDetail(next, "issuer_name", bank.name[locale]);
            const named = replaceDetail(next, "bank_name", bank.name[locale]);
            const identified = bank.swiftPrefix
                ? replaceDetail(named, "swift_bic", bank.swiftPrefix)
                : named;
            return replaceDetail(identified, "bank_region", bank.region);
        });
        setBankSearch("");
        setLocalError(null);
    }

    function toggleAsset(assetId: string, checked: boolean) {
        setAssetsTouched(true);
        setAssetIds((current) =>
            checked ? [...current, assetId] : current.filter((item) => item !== assetId),
        );
        setLocalError(null);
    }

    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (busy) return;
        if (!name.trim() || containsControlCharacters(name)) {
            setLocalError("name");
            return;
        }
        if (!assetIds.length || assetIds.length > 100) {
            setLocalError("assets");
            return;
        }
        const assetsChanged =
            !account ||
            assetIds.length !== originalAssetIds.length ||
            assetIds.some((assetId) => !originalAssetIds.includes(assetId));
        if (
            assetsChanged &&
            assetIds.some(
                (assetId) => !assets.some((asset) => asset.asset_id === assetId && asset.enabled),
            )
        ) {
            setLocalError("disabled");
            return;
        }
        const changed = (key: AccountDetailKey) =>
            (values.get(key) ?? "") !== (account?.details[key] ?? "");
        if (card) {
            if (cardNumber && !validCardNumber(cardNumber)) {
                setLocalError("card");
                return;
            }
            if (
                !cardNumber &&
                changed("card_last_four") &&
                !validCardLastFour(values.get("card_last_four") ?? "")
            ) {
                setLocalError("suffix");
                return;
            }
            if (changed("expiry") && !validCardExpiry(values.get("expiry") ?? "")) {
                setLocalError("expiry");
                return;
            }
            if (
                kind === "credit_card" &&
                changed("credit_limit") &&
                !validCreditLimit(values.get("credit_limit") ?? "")
            ) {
                setLocalError("limit");
                return;
            }
        }
        if (
            kind === "credit_card" &&
            (["statement_day", "payment_due_day"] as const).some(
                (key) => changed(key) && !validCardDay(values.get(key) ?? ""),
            )
        ) {
            setLocalError("day");
            return;
        }
        if (
            kind === "crypto" &&
            (changed("wallet_network") || changed("wallet_address")) &&
            !validWalletAddress(
                values.get("wallet_network") ?? "",
                values.get("wallet_address") ?? "",
            )
        ) {
            setLocalError("wallet");
            return;
        }
        let details: Record<string, string>;
        try {
            details = compileDetails(rows, ACCOUNT_DETAIL_KEYS);
            // Some keys were user-defined before the typed fields existed.
            for (const [key, value] of Object.entries(account?.details ?? {})) {
                if (value === "" && values.get(key) === value && !Object.hasOwn(details, key))
                    details[key] = value;
            }
            if (Object.keys(details).length > 32) throw new Error("too-many-details");
        } catch {
            setLocalError("details");
            return;
        }
        setLocalError(null);
        const privateNumber = card && cardNumber ? { card_number: cardNumber } : {};
        if (account && expectedVersion !== undefined)
            onSubmit({
                name: name.trim(),
                details,
                expected_version: expectedVersion,
                ...(kind !== account.kind ? { kind: kind as AccountKind } : {}),
                ...(assetsChanged ? { asset_ids: assetIds } : {}),
                ...privateNumber,
            });
        else
            onSubmit({
                id,
                name: name.trim(),
                kind: kind as AccountKind,
                details,
                asset_ids: assetIds,
                ...privateNumber,
            });
    }

    function selectField(
        field: AccountDetailKey,
        options: Record<string, LocalizedLabel>,
        value: string,
        onChange: (value: string) => void,
        locked = false,
    ) {
        return (
            <select
                id={`${prefix}-${field}`}
                value={value}
                onChange={(event) => onChange(event.target.value)}
                disabled={busy || locked}
            >
                <option value="">{t.emptyChoice}</option>
                {value && !Object.hasOwn(options, value) && <option value={value}>{value}</option>}
                {Object.entries(options).map(([id, label]) => (
                    <option key={id} value={id}>
                        {label[locale]}
                    </option>
                ))}
            </select>
        );
    }

    const effectiveNetwork = values.get("wallet_network") ?? "";
    function renderControl(key: AccountDetailKey, value: string) {
        const update = (next: string) => setField(key, next);
        if (key === "bank_region")
            return selectField(key, BANK_REGIONS, bankRegion, update, !!institution);
        if (key === "wallet_storage") return selectField(key, WALLET_STORAGE, value, update);
        if (key === "wallet_network")
            return selectField(
                key,
                {
                    ...WALLET_NETWORKS,
                    other: { zh: labels.zh.otherNetwork, en: labels.en.otherNetwork },
                },
                otherWallet ? "other" : effectiveNetwork,
                (next) => {
                    setOtherWallet(next === "other");
                    update(next === "other" ? "" : next);
                },
            );
        const numeric = [
            "card_last_four",
            "statement_day",
            "payment_due_day",
            "cnaps_code",
            "routing_number",
            "hk_bank_code",
        ];
        return (
            <input
                id={`${prefix}-${key}`}
                value={value}
                disabled={busy || (key === "issuer_name" && !!institution)}
                type={key === "email" ? "email" : "text"}
                inputMode={
                    numeric.includes(key)
                        ? "numeric"
                        : key === "credit_limit"
                          ? "decimal"
                          : undefined
                }
                autoComplete={
                    ["card_last_four", "wallet_address", "expiry", "cardholder"].includes(key)
                        ? "off"
                        : undefined
                }
                placeholder={key === "expiry" ? "MM/YY" : undefined}
                maxLength={2000}
                onChange={(event) => update(event.target.value)}
            />
        );
    }

    function renderField(key: AccountDetailKey) {
        const value = values.get(key) ?? "";
        if (key === "institution") {
            if (card) {
                return (
                    <CardIssuerPicker
                        key={key}
                        locale={locale}
                        institutionId={value}
                        onChange={selectBank}
                    />
                );
            }
            return (
                <div key={key} className="account-institution">
                    <div className="field">
                        <label htmlFor={`${prefix}-bank-search`}>{t.searchBank}</label>
                        <input
                            id={`${prefix}-bank-search`}
                            value={bankSearch}
                            onChange={(event) => setBankSearch(event.target.value)}
                            disabled={busy}
                        />
                    </div>
                    <div className="field">
                        <label htmlFor={`${prefix}-${key}`}>
                            {ACCOUNT_DETAILS[key].label[locale]}
                        </label>
                        <div className="account-picker-select">
                            <span className="account-picker-selected" aria-hidden="true">
                                <AccountIcon kind="bank" institution={institution?.id} compact />
                            </span>
                            <select
                                id={`${prefix}-${key}`}
                                value={value}
                                disabled={busy}
                                onChange={(event) => selectBank(event.target.value)}
                            >
                                <option value="">{t.otherBank}</option>
                                {value && !institution && <option value={value}>{value}</option>}
                                {banks.map((bank) => (
                                    <option
                                        key={bank.id}
                                        value={bank.id}
                                        label={bank.name[locale]}
                                        aria-label={bank.name[locale]}
                                    >
                                        <AccountIcon kind="bank" institution={bank.id} compact />
                                        <span>{bank.name[locale]}</span>
                                    </option>
                                ))}
                            </select>
                        </div>
                    </div>
                </div>
            );
        }
        if (key === "card_last_four" && (account?.has_card_number || cardNumber)) return null;

        return (
            <div className="field" key={key}>
                <label htmlFor={`${prefix}-${key}`}>{ACCOUNT_DETAILS[key].label[locale]}</label>
                {renderControl(key, value)}
                {key === "bank_region" && institution && (
                    <p className="help-text">{t.institutionRegion}</p>
                )}
                {key === "wallet_network" && otherWallet && (
                    <div className="field">
                        <label htmlFor={`${prefix}-custom-network`}>{t.customNetwork}</label>
                        <input
                            id={`${prefix}-custom-network`}
                            maxLength={64}
                            disabled={busy}
                            value={effectiveNetwork}
                            onChange={(event) => setField(key, event.target.value)}
                        />
                    </div>
                )}
                {key === "wallet_address" && <p className="help-text">{t.publicAddress}</p>}
            </div>
        );
    }

    return (
        <form
            onSubmit={submit}
            aria-busy={busy}
            aria-label={locale === "zh" ? "账户资料表单" : "Account details form"}
        >
            <div className="account-drawer-fields">
                <div className="field">
                    <label htmlFor={`${prefix}-name`}>{t.name}</label>
                    <input
                        id={`${prefix}-name`}
                        required
                        maxLength={160}
                        value={name}
                        disabled={busy}
                        onChange={(event) => {
                            setName(event.target.value);
                            setLocalError(null);
                        }}
                    />
                </div>
                <fieldset className="form-section" disabled={busy}>
                    <legend>{t.kind}</legend>
                    <div className="account-type-grid">
                        {ACCOUNT_KINDS.map((value) => (
                            <label key={value} className="account-type-option">
                                <input
                                    type="radio"
                                    name={`${prefix}-kind`}
                                    value={value}
                                    checked={kind === value}
                                    onChange={() => changeKind(value)}
                                />
                                <AccountIcon kind={value} compact />
                                <span>{t.kinds[value]}</span>
                            </label>
                        ))}
                    </div>
                    {kind === "debit_card" && <p className="help-text">{t.debitHelp}</p>}
                </fieldset>
                <fieldset
                    className="form-section"
                    disabled={busy}
                    aria-describedby={`${prefix}-asset-help`}
                >
                    <legend>{t.assets}</legend>
                    <p className="help-text" id={`${prefix}-asset-help`}>
                        {t.assetHelp}
                    </p>
                    <label className="account-show-assets">
                        <input
                            type="checkbox"
                            checked={showAll}
                            onChange={(event) => setShowAll(event.target.checked)}
                        />
                        {t.showAll}
                    </label>
                    <div className="check-grid">
                        {availableAssets.map((asset) => (
                            <label key={asset.asset_id}>
                                <input
                                    type="checkbox"
                                    checked={assetIds.includes(asset.asset_id)}
                                    value={asset.asset_id}
                                    aria-label={asset.asset_id}
                                    onChange={(event) =>
                                        toggleAsset(asset.asset_id, event.target.checked)
                                    }
                                />
                                <AssetIcon asset={asset} compact />
                                <span>
                                    {asset.code}
                                    {asset.network ? ` · ${asset.network}` : ""}
                                    {asset.token_reference ? ` · ${asset.token_reference}` : ""}
                                    {!asset.enabled ? ` · ${t.disabled}` : ""}
                                </span>
                            </label>
                        ))}
                        {missingAssetIds.map((assetId) => (
                            <label key={assetId}>
                                <input
                                    type="checkbox"
                                    checked={assetIds.includes(assetId)}
                                    value={assetId}
                                    aria-label={assetId}
                                    onChange={(event) => toggleAsset(assetId, event.target.checked)}
                                />
                                <span>
                                    {assetId} · {t.missing}
                                </span>
                            </label>
                        ))}
                    </div>
                    {!availableAssets.length && !missingAssetIds.length && (
                        <p className="help-text">{t.noAssets}</p>
                    )}
                </fieldset>
                <fieldset className="form-section" disabled={busy}>
                    <legend>{t.details}</legend>
                    {suggestedInstitution && (
                        <div className="institution-suggestion">
                            <p className="help-text">
                                {locale === "zh" ? "关联到" : "Link to"} ·{" "}
                                {suggestedInstitution.name[locale]}
                            </p>
                            <button
                                type="button"
                                className="secondary-button"
                                onClick={() => {
                                    setField("institution", suggestedInstitution.id);
                                }}
                            >
                                {locale === "zh" ? "确认关联" : "Confirm association"}
                            </button>
                        </div>
                    )}
                    {card && (
                        <div className="field">
                            <label htmlFor={`${prefix}-card-number`}>{t.cardNumber}</label>
                            <input
                                id={`${prefix}-card-number`}
                                type="password"
                                autoComplete="off"
                                inputMode="numeric"
                                spellCheck={false}
                                value={cardNumber}
                                onChange={(event) => {
                                    setCardNumber(event.target.value);
                                    setLocalError(null);
                                }}
                                aria-describedby={`${prefix}-card-help`}
                            />
                            <p className="help-text" id={`${prefix}-card-help`}>
                                {t.cardHelp}
                            </p>
                        </div>
                    )}
                    <div className="form-grid">{fields.map(renderField)}</div>
                </fieldset>
                <details className="account-extra-details">
                    <summary>
                        {hidden.length || custom.length ? t.otherDetails : t.customDetails}
                    </summary>
                    {hidden.length > 0 && (
                        <dl className="profile-details">
                            {hidden.map((row) => (
                                <div key={row.id}>
                                    <dt>
                                        {ACCOUNT_DETAILS[row.key as AccountDetailKey].label[locale]}
                                    </dt>
                                    <dd>
                                        {row.key === "institution"
                                            ? (getInstitution(row.value)?.name[locale] ?? row.value)
                                            : row.value}
                                    </dd>
                                </div>
                            ))}
                        </dl>
                    )}
                    <DetailEditor
                        locale={locale}
                        rows={custom}
                        fields={[]}
                        busy={busy}
                        onChange={(next) => {
                            setRows((prior) => [...prior.filter((row) => !row.custom), ...next]);
                            setLocalError(null);
                        }}
                        prefix={`${prefix}-extra`}
                    />
                </details>
            </div>
            {(localError || error) && (
                <p className="inline-error" role="alert">
                    {localError ? t[`${localError}Error`] : error}
                </p>
            )}
            <div className="form-actions">
                {onReload && (
                    <button
                        type="button"
                        className="secondary-button"
                        disabled={busy}
                        onClick={onReload}
                    >
                        {t.reload}
                    </button>
                )}
                <button
                    type="button"
                    className="secondary-button"
                    disabled={busy}
                    onClick={onCancel}
                >
                    {t.cancel}
                </button>
                <button type="submit" className="primary-button" disabled={busy}>
                    {busy ? t.saving : t.save}
                </button>
            </div>
        </form>
    );
}
