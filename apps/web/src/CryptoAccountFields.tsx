import { useId, useState } from "react";
import { AccountIcon } from "./AccountIcon";
import { AssetIcon } from "./AssetIcon";
import { assetNetworkName } from "./asset-networks";
import { coinDraft, OTHER_PLATFORM } from "./crypto-account-draft";
import type { CoinDraft, CryptoAccountDraft, CryptoAssetChoice } from "./crypto-account-draft";
import type { Locale } from "./i18n";
import { CRYPTO_PLATFORMS } from "./institutions";
import type { Account, AssetPreset } from "./ledger-api";
import "./crypto-account.css";

export type Custody = NonNullable<Account["custody_mode"]>;
type Translate = (zh: string, en: string) => string;

function TextField({
    label,
    value,
    change,
    maxLength,
    type = "text",
}: {
    label: string;
    value: string;
    change: (value: string) => void;
    maxLength: number;
    type?: "text" | "email";
}) {
    const id = useId();
    return (
        <div className="field">
            <label htmlFor={id}>{label}</label>
            <input
                id={id}
                type={type}
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                maxLength={maxLength}
                value={value}
                onChange={(event) => change(event.currentTarget.value)}
            />
        </div>
    );
}

function NetworkField({
    locale,
    label,
    networks,
    value,
    change,
}: {
    locale: Locale;
    label: string;
    networks: string[];
    value: string;
    change: (value: string) => void;
}) {
    const id = useId();
    return (
        <div className="field">
            <label htmlFor={id}>{label}</label>
            <select id={id} value={value} onChange={(event) => change(event.currentTarget.value)}>
                <option value="">{locale === "zh" ? "请选择" : "Choose"}</option>
                {value && !networks.includes(value) && (
                    <option value={value}>{assetNetworkName(value, locale)}</option>
                )}
                {networks.map((network) => (
                    <option key={network} value={network}>
                        {assetNetworkName(network, locale)}
                    </option>
                ))}
            </select>
        </div>
    );
}

function CoinDetails({
    asset,
    draft,
    custody,
    networks,
    locale,
    t,
    change,
}: {
    asset: CryptoAssetChoice;
    draft: CoinDraft;
    custody: Custody;
    networks: string[];
    locale: Locale;
    t: Translate;
    change: (value: CoinDraft) => void;
}) {
    return (
        <details className="crypto-coin-details" data-testid={`coin-profile-${asset.asset_id}`}>
            <summary>
                <span className="icon-label">
                    <AssetIcon asset={asset} compact />
                    <strong>{asset.code}</strong>
                    {asset.network && <small>{assetNetworkName(asset.network, locale)}</small>}
                </span>
            </summary>
            <div className="form-grid">
                {custody === "custodial" && (
                    <NetworkField
                        locale={locale}
                        label={t("充值网络", "Deposit network")}
                        networks={networks}
                        value={draft.network}
                        change={(network) => change({ ...draft, network })}
                    />
                )}
                <TextField
                    label={t("充值地址", "Deposit address")}
                    value={draft.address}
                    change={(address) => change({ ...draft, address })}
                    maxLength={128}
                />
                <TextField
                    label="Memo / Tag"
                    value={draft.memo}
                    change={(memo) => change({ ...draft, memo })}
                    maxLength={256}
                />
                <TextField
                    label={t("标签", "Label")}
                    value={draft.label}
                    change={(label) => change({ ...draft, label })}
                    maxLength={160}
                />
            </div>
        </details>
    );
}

export function CryptoAccountFields({
    locale,
    busy,
    custody,
    assets,
    assetIds,
    presets,
    onRetryPresets,
    draft,
    changeMode,
    selectAsset,
    change,
}: {
    locale: Locale;
    busy: boolean;
    custody: Custody | null;
    assets: CryptoAssetChoice[];
    assetIds: string[];
    presets: AssetPreset[] | undefined;
    onRetryPresets?: () => void;
    draft: CryptoAccountDraft;
    changeMode: (mode: Custody) => void;
    selectAsset: (id: string, checked: boolean) => void;
    change: (value: CryptoAccountDraft) => void;
}) {
    const t: Translate = (zh, en) => (locale === "zh" ? zh : en);
    const id = useId();
    const [query, setQuery] = useState("");
    const [network, setNetwork] = useState("");
    const word = query.trim().normalize("NFKC").toLowerCase();
    const selected = assets.filter((asset) => assetIds.includes(asset.asset_id));
    const custodyKind = custody === "onchain" ? "self-custody" : custody;
    const registry = CRYPTO_PLATFORMS.filter((item) => item.custody === custodyKind);
    const institution = registry.find((item) => item.id === draft.platform);
    const configured =
        !!institution || (draft.platform === OTHER_PLATFORM && !!draft.customName.trim());
    const allowed = (asset: CryptoAssetChoice) =>
        custody === "custodial"
            ? asset.kind === "custodial"
            : asset.kind === "token" || asset.kind === "native";
    const visiblePlatforms = registry.filter(
        (item) =>
            item.id === draft.platform ||
            [item.name.zh, item.name.en, item.shortName, ...item.aliases].some((value) =>
                value.normalize("NFKC").toLowerCase().includes(word),
            ),
    );
    const networks = [
        ...new Set(
            assets.flatMap((asset) =>
                asset.enabled && allowed(asset) && asset.network ? [asset.network] : [],
            ),
        ),
    ].sort();
    // Checked assets stay visible when switching networks or reviewing legacy selections.
    const visibleAssets = assets.filter(
        (asset) =>
            assetIds.includes(asset.asset_id) ||
            (asset.enabled &&
                allowed(asset) &&
                (custody === "custodial" || asset.network === network)),
    );
    const missing = assetIds.filter(
        (assetId) => !selected.some((asset) => asset.asset_id === assetId),
    );

    function pickMode(mode: Custody) {
        if (custody === mode) return;
        changeMode(mode);
        setQuery("");
        setNetwork("");
    }
    function pickPlatform(platform: string) {
        change({ ...draft, platform });
        setQuery("");
    }
    function changeCoin(asset: CryptoAssetChoice, value: CoinDraft) {
        change({ ...draft, coins: { ...draft.coins, [asset.asset_id]: value } });
    }

    return (
        <div className="crypto-account-fields">
            <fieldset className="form-section" disabled={busy}>
                <legend>{t("托管方式", "Custody mode")}</legend>
                <div className="crypto-mode-grid">
                    <label>
                        <input
                            name={`${id}-mode`}
                            type="radio"
                            value="custodial"
                            checked={custody === "custodial"}
                            onChange={() => pickMode("custodial")}
                        />
                        {t("托管平台", "Custodial platform")}
                    </label>
                    <label>
                        <input
                            name={`${id}-mode`}
                            type="radio"
                            value="onchain"
                            checked={custody === "onchain"}
                            onChange={() => pickMode("onchain")}
                        />
                        {t("自托管钱包", "Self-custody wallet")}
                    </label>
                </div>
            </fieldset>
            {custody && (
                <fieldset className="form-section" disabled={busy}>
                    <legend>{t("平台", "Platform")}</legend>
                    <div className="field">
                        <label htmlFor={`${id}-search`}>{t("搜索平台", "Search platforms")}</label>
                        <input
                            id={`${id}-search`}
                            type="search"
                            value={query}
                            autoComplete="off"
                            onChange={(event) => setQuery(event.currentTarget.value)}
                        />
                    </div>
                    <div className="card-issuer-list crypto-platform-list">
                        {visiblePlatforms.map((item) => (
                            <label key={item.id}>
                                <input
                                    name={`${id}-platform`}
                                    type="radio"
                                    value={item.id}
                                    checked={institution?.id === item.id}
                                    onChange={() => pickPlatform(item.id)}
                                />
                                <AccountIcon kind="crypto" institution={item.id} compact />
                                <span>{item.name[locale]}</span>
                            </label>
                        ))}
                        <label>
                            <input
                                name={`${id}-platform`}
                                type="radio"
                                value={OTHER_PLATFORM}
                                checked={draft.platform === OTHER_PLATFORM}
                                onChange={() => pickPlatform(OTHER_PLATFORM)}
                            />
                            <AccountIcon
                                kind="crypto"
                                customName={draft.customName || undefined}
                                compact
                            />
                            <span>{t("其他", "Other")}</span>
                        </label>
                    </div>
                    {draft.platform === OTHER_PLATFORM && (
                        <TextField
                            label={t("平台名称", "Platform name")}
                            value={draft.customName}
                            change={(customName) => change({ ...draft, customName })}
                            maxLength={80}
                        />
                    )}
                </fieldset>
            )}
            {custody && configured && (
                <>
                    <fieldset className="form-section" disabled={busy}>
                        <legend>{t("币种", "Coins")}</legend>
                        {custody === "onchain" && (
                            <NetworkField
                                locale={locale}
                                label={t("选择网络", "Choose network")}
                                networks={networks}
                                value={network}
                                change={setNetwork}
                            />
                        )}
                        <div className="check-grid">
                            {visibleAssets.map((asset) => (
                                <label key={asset.asset_id}>
                                    <input
                                        type="checkbox"
                                        aria-label={asset.asset_id}
                                        value={asset.asset_id}
                                        checked={assetIds.includes(asset.asset_id)}
                                        onChange={(event) =>
                                            selectAsset(asset.asset_id, event.currentTarget.checked)
                                        }
                                    />
                                    <AssetIcon asset={asset} compact />
                                    <span>
                                        {asset.code}
                                        {asset.network && (
                                            <> · {assetNetworkName(asset.network, locale)}</>
                                        )}
                                        {asset.kind === "token" && !asset.verified && (
                                            <> · {t("自定义·未核对", "Custom · Unverified")}</>
                                        )}
                                        {!allowed(asset) && (
                                            <> · {t("托管方式不匹配", "Incompatible custody")}</>
                                        )}
                                        {!asset.enabled && <> · {t("已停用", "Disabled")}</>}
                                    </span>
                                </label>
                            ))}
                            {missing.map((assetId) => (
                                <label key={assetId}>
                                    <input
                                        type="checkbox"
                                        checked
                                        value={assetId}
                                        aria-label={assetId}
                                        onChange={(event) =>
                                            selectAsset(assetId, event.currentTarget.checked)
                                        }
                                    />
                                    <span>
                                        {assetId} · {t("未载入", "Not loaded")}
                                    </span>
                                </label>
                            ))}
                        </div>
                        {!presets && (
                            <p className="help-text" role="status">
                                {onRetryPresets
                                    ? t("无法读取内置币种。", "Built-in coins could not be loaded.")
                                    : t("正在读取内置币种…", "Loading built-in coins…")}
                            </p>
                        )}
                        {onRetryPresets && (
                            <button
                                type="button"
                                className="secondary-button"
                                onClick={onRetryPresets}
                            >
                                {t("重新读取内置币种", "Reload built-in coins")}
                            </button>
                        )}
                        {presets && visibleAssets.length === 0 && (
                            <p className="help-text">
                                {custody === "onchain" && !network && networks.length > 0
                                    ? t(
                                          "选择网络后查看可用币种。",
                                          "Choose a network to see its assets.",
                                      )
                                    : t(
                                          "当前没有可用币种，请先在资产目录添加。",
                                          "No coins available. Add assets in the asset catalog first.",
                                      )}
                            </p>
                        )}
                    </fieldset>
                    {selected.length > 0 && (
                        <>
                            <fieldset className="form-section" disabled={busy}>
                                <legend>{t("平台资料", "Platform details")}</legend>
                                <div className="form-grid">
                                    <TextField
                                        label={t("UID 或账户名", "UID or account name")}
                                        value={draft.uid}
                                        change={(uid) => change({ ...draft, uid })}
                                        maxLength={160}
                                    />
                                    <TextField
                                        label={t("绑定邮箱", "Linked email")}
                                        value={draft.email}
                                        change={(email) => change({ ...draft, email })}
                                        maxLength={254}
                                        type="email"
                                    />
                                    <TextField
                                        label={t("备注", "Notes")}
                                        value={draft.note}
                                        change={(note) => change({ ...draft, note })}
                                        maxLength={1000}
                                    />
                                </div>
                            </fieldset>
                            <fieldset className="form-section" disabled={busy}>
                                <legend>{t("币种资料", "Coin details")}</legend>
                                <p className="help-text">
                                    {t(
                                        "请只填写公开充值地址。每个币种的资料独立保存。",
                                        "Enter public deposit addresses only. Each coin keeps its own details.",
                                    )}
                                </p>
                                {selected
                                    .filter((asset) => asset.enabled && allowed(asset))
                                    .map((asset) => (
                                        <CoinDetails
                                            key={asset.asset_id}
                                            asset={asset}
                                            locale={locale}
                                            t={t}
                                            custody={custody}
                                            draft={draft.coins[asset.asset_id] ?? coinDraft()}
                                            networks={(presets ?? []).flatMap((preset) =>
                                                preset.code === asset.code && preset.network
                                                    ? [preset.network]
                                                    : [],
                                            )}
                                            change={(value) => changeCoin(asset, value)}
                                        />
                                    ))}
                            </fieldset>
                        </>
                    )}
                </>
            )}
        </div>
    );
}
