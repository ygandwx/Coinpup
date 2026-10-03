import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { Account, AccountCreateBody, AccountUpdateBody, Asset } from "./ledger-api";
import { DetailEditor } from "./EntityForm";
import {
    ACCOUNT_DETAIL_FIELDS,
    compileDetails,
    containsControlCharacters,
    detailRows,
} from "./regions";

const ACCOUNT_KINDS = [
    "bank",
    "cash",
    "wechat",
    "alipay",
    "credit_card",
    "paypal",
    "wise",
    "stripe",
    "crypto",
] as const;
type AccountKind = (typeof ACCOUNT_KINDS)[number];
const ACCOUNT_KEYS = new Set(ACCOUNT_DETAIL_FIELDS.map((field) => field.key));
const labels = {
    zh: {
        name: "账户名称",
        kind: "账户类型",
        assets: "资产",
        details: "账户资料",
        save: "保存",
        saving: "保存中…",
        cancel: "取消",
        assetHelp: "至少选择一种资产。法币和加密资产均按原资产保存，不自动换算。",
        disabled: "已停用",
        missing: "未载入",
        noAssets: "暂无可用资产，请先配置资产目录。",
        detailHelp: "按需要填写银行、支付账户或钱包资料，其他字段可以留空。",
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
        assetHelp:
            "Select at least one asset. Fiat and crypto holdings retain their original asset; no conversion is applied.",
        disabled: "Disabled",
        missing: "Not loaded",
        noAssets: "No assets are available. Configure the asset catalog first.",
        detailHelp:
            "Add bank, payment account or wallet details as needed. Other fields can remain blank.",
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
    busy: boolean;
    error?: string | null;
    onSubmit: (body: AccountCreateBody | AccountUpdateBody) => void;
    onCancel: () => void;
};

export function AccountForm({
    locale,
    assets,
    account,
    busy,
    error,
    onSubmit,
    onCancel,
}: AccountFormProps) {
    const t = labels[locale];
    const prefix = useId();
    const [id] = useState(() => crypto.randomUUID());
    const [expectedVersion] = useState(account?.version);
    const [originalAssetIds] = useState(() => [...(account?.asset_ids ?? [])]);
    const [name, setName] = useState(account?.name ?? "");
    const [kind, setKind] = useState<AccountKind>(
        (account?.kind as AccountKind | undefined) ?? "bank",
    );
    const [assetIds, setAssetIds] = useState<string[]>(() => [...(account?.asset_ids ?? [])]);
    const [rows, setRows] = useState(() => detailRows(account?.details ?? {}, ACCOUNT_KEYS));
    const [localError, setLocalError] = useState<"name" | "assets" | "disabled" | "details" | null>(
        null,
    );
    const availableAssets = assets.filter(
        (asset) => asset.enabled || account?.asset_ids.includes(asset.asset_id),
    );
    const missingAssetIds = (account?.asset_ids ?? []).filter(
        (assetId) => !assets.some((asset) => asset.asset_id === assetId),
    );

    function toggleAsset(assetId: string, checked: boolean) {
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
        let details: Record<string, string>;
        try {
            details = compileDetails(rows, ACCOUNT_KEYS);
        } catch {
            setLocalError("details");
            return;
        }
        setLocalError(null);
        if (account && expectedVersion !== undefined)
            onSubmit({
                name: name.trim(),
                kind,
                details,
                expected_version: expectedVersion,
                ...(assetsChanged ? { asset_ids: assetIds } : {}),
            });
        else onSubmit({ id, name: name.trim(), kind, details, asset_ids: assetIds });
    }

    return (
        <form
            onSubmit={submit}
            aria-busy={busy}
            aria-label={locale === "zh" ? "账户资料表单" : "Account details form"}
        >
            <div className="form-grid">
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
                <div className="field">
                    <label htmlFor={`${prefix}-kind`}>{t.kind}</label>
                    <select
                        id={`${prefix}-kind`}
                        value={kind}
                        disabled={busy}
                        onChange={(event) => setKind(event.target.value as AccountKind)}
                    >
                        {ACCOUNT_KINDS.map((value) => (
                            <option key={value} value={value}>
                                {t.kinds[value]}
                            </option>
                        ))}
                    </select>
                </div>
            </div>
            <fieldset
                className="form-section"
                disabled={busy}
                aria-describedby={`${prefix}-asset-help`}
            >
                <legend>{t.assets}</legend>
                <p className="help-text" id={`${prefix}-asset-help`}>
                    {t.assetHelp}
                </p>
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
            <section className="form-section">
                <h3>{t.details}</h3>
                <p className="help-text">{t.detailHelp}</p>
            </section>
            <DetailEditor
                locale={locale}
                rows={rows}
                fields={ACCOUNT_DETAIL_FIELDS}
                busy={busy}
                onChange={setRows}
                prefix={`${prefix}-details`}
            />
            {(localError || error) && (
                <p className="inline-error" role="alert">
                    {localError ? t[`${localError}Error`] : error}
                </p>
            )}
            <div className="form-actions">
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
