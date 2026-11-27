import { AssetIcon } from "./AssetIcon";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import { AssetForm } from "./AssetForm";
import { assetIdentity } from "./asset-identity";
import { assetNetworkName } from "./asset-networks";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";
import { createAsset, listAssetPresets, listAssets, updateAsset } from "./ledger-api";
import type { Asset, AssetCreate, AssetPreset } from "./ledger-api";

export function AssetsPanel({
    locale,
    session,
    assets,
    loading,
    onChanged,
    onUnauthorized,
    onEditingChange,
}: {
    locale: Locale;
    session: Session;
    assets: Asset[];
    loading: boolean;
    onChanged: () => void;
    onUnauthorized: () => void;
    onEditingChange: (value: boolean) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [editing, setEditing] = useState<"manual" | "preset" | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<unknown>(null);
    const [saved, setSaved] = useState(false);
    const [unconfirmed, setUnconfirmed] = useState<AssetCreate | null>(null);
    const [presetRequest, setPresetRequest] = useState(0);
    const [presetResponse, setPresetResponse] = useState<
        { presets: AssetPreset[]; request: number } | { error: unknown; request: number } | null
    >(null);
    const activePresets = presetResponse?.request === presetRequest ? presetResponse : null;
    const callbacks = useRef({ onChanged, onUnauthorized, onEditingChange });
    callbacks.current = { onChanged, onUnauthorized, onEditingChange };
    useEffect(() => {
        callbacks.current.onEditingChange(!!editing || busy);
        return () => callbacks.current.onEditingChange(false);
    }, [editing, busy]);
    useEffect(() => {
        if (editing) document.getElementById("asset-panel-title")?.focus();
    }, [editing]);
    useEffect(() => {
        if (!editing || activePresets) return;
        const abort = new AbortController();
        void listAssetPresets(abort.signal).then(
            (presets) => {
                if (!abort.signal.aborted) setPresetResponse({ presets, request: presetRequest });
            },
            (error: unknown) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.kind === "unauthorized")
                    callbacks.current.onUnauthorized();
                else setPresetResponse({ error, request: presetRequest });
            },
        );
        return () => abort.abort();
    }, [activePresets, editing, presetRequest]);
    function failure(problem: unknown) {
        if (problem instanceof ApiError && problem.kind === "unauthorized")
            callbacks.current.onUnauthorized();
        else setError(problem);
    }
    function success() {
        setEditing(null);
        setUnconfirmed(null);
        setSaved(true);
        setError(null);
        callbacks.current.onChanged();
        requestAnimationFrame(() => document.getElementById("new-asset-button")?.focus());
    }
    async function save(body: AssetCreate) {
        if (busy || unconfirmed) return;
        setBusy(true);
        setError(null);
        setSaved(false);
        try {
            await createAsset(session.csrf_token, body);
            success();
        } catch (problem) {
            if (
                problem instanceof ApiError &&
                (problem.kind === "network" ||
                    problem.status === null ||
                    problem.status >= 500 ||
                    problem.code === "invalid_response")
            )
                setUnconfirmed(body);
            failure(problem);
        } finally {
            setBusy(false);
        }
    }
    async function checkCreation() {
        if (busy || !unconfirmed) return;
        setBusy(true);
        setError(null);
        try {
            const identity = assetIdentity({
                ...unconfirmed,
                network: unconfirmed.network ?? null,
                token_reference: unconfirmed.token_reference ?? null,
            });
            if (!identity) throw new ApiError("server", null, "invalid_response");
            let found: Asset | undefined;
            for (let offset = 0; offset <= 100000; offset += 100) {
                const page = await listAssets({ include_disabled: true, limit: 100, offset });
                found = page.find((asset) => asset.asset_id === identity);
                if (found || page.length < 100) break;
                if (offset === 100000) throw new ApiError("server");
            }
            callbacks.current.onChanged();
            if (
                found &&
                found.code === unconfirmed.code &&
                found.kind === unconfirmed.kind &&
                found.scale === unconfirmed.scale &&
                found.enabled === (unconfirmed.enabled ?? true)
            )
                success();
            else {
                setUnconfirmed(null);
                setError(new ApiError("server", 409, "asset_recheck"));
            }
        } catch (problem) {
            failure(problem);
        } finally {
            setBusy(false);
        }
    }
    async function toggle(asset: Asset) {
        if (busy || loading) return;
        setBusy(true);
        setError(null);
        setSaved(false);
        try {
            await updateAsset(session.csrf_token, asset.asset_id, {
                expected_version: asset.version,
                enabled: !asset.enabled,
            });
            setSaved(true);
        } catch (problem) {
            failure(problem);
        } finally {
            callbacks.current.onChanged();
            setBusy(false);
        }
    }
    return (
        <section className="business-panel">
            <div className="section-heading">
                <div>
                    <h2 id="asset-panel-title" tabIndex={-1}>
                        {editing ? t("新增资产", "New asset") : t("资产目录", "Asset catalog")}
                    </h2>
                    <p className="help-text">
                        {t(
                            "目录由所有账本共用；账户分别选择支持的资产。资产身份和精度保存后固定。",
                            "All ledgers share this catalog; each account selects its supported assets. Asset identity and precision stay fixed after creation.",
                        )}
                    </p>
                </div>
                {!editing && (
                    <div className="asset-create-actions">
                        {(["manual", "preset"] as const).map((mode) => (
                            <button
                                key={mode}
                                id={mode === "manual" ? "new-asset-button" : undefined}
                                className={
                                    mode === "manual" ? "primary-button" : "secondary-button"
                                }
                                disabled={busy || loading}
                                onClick={() => {
                                    setEditing(mode);
                                    setSaved(false);
                                    setError(null);
                                }}
                            >
                                {mode === "manual"
                                    ? t("新增资产", "New asset")
                                    : t("从预设添加", "Add from preset")}
                            </button>
                        ))}
                    </div>
                )}
            </div>
            {saved && (
                <p className="save-notice" role="status">
                    {t("资产配置已保存。", "Asset configuration saved.")}
                </p>
            )}
            {unconfirmed && (
                <div className="pending-command" role="status">
                    <p>
                        {t(
                            "创建结果待确认。先刷新目录核对相同身份的资产，再继续。",
                            "Asset creation is unconfirmed. Check the catalog for this identity before continuing.",
                        )}
                    </p>
                    <button
                        className="secondary-button"
                        disabled={busy}
                        onClick={() => void checkCreation()}
                    >
                        {t("核对资产目录", "Check asset catalog")}
                    </button>
                </div>
            )}
            {editing ? (
                <AssetForm
                    locale={locale}
                    fromPreset={editing === "preset"}
                    busy={busy || !!unconfirmed}
                    presets={
                        activePresets && "presets" in activePresets
                            ? activePresets.presets
                            : undefined
                    }
                    presetError={
                        activePresets && "error" in activePresets
                            ? businessError(activePresets.error, locale)
                            : undefined
                    }
                    onRetryPresets={() => setPresetRequest((value) => value + 1)}
                    error={error ? businessError(error, locale) : undefined}
                    onSubmit={(body) => void save(body)}
                    onCancel={() => {
                        if (!busy && !unconfirmed) {
                            setEditing(null);
                            setError(null);
                            requestAnimationFrame(() =>
                                document.getElementById("new-asset-button")?.focus(),
                            );
                        }
                    }}
                />
            ) : (
                <>
                    {error !== null && (
                        <p className="inline-error" role="alert">
                            {businessError(error, locale)}
                        </p>
                    )}
                    <div className="asset-catalog">
                        {assets.map((asset) => (
                            <article
                                className="asset-card"
                                data-testid={`asset-${asset.asset_id}`}
                                key={asset.asset_id}
                            >
                                <div className="record-heading">
                                    <h3 className="icon-label">
                                        <AssetIcon asset={asset} />
                                        {asset.code}
                                    </h3>
                                    <span className="record-badge">
                                        {asset.enabled
                                            ? t("已启用", "Enabled")
                                            : t("已停用", "Disabled")}
                                    </span>
                                </div>
                                {asset.kind === "token" && !asset.verified && (
                                    <p className="asset-verification">
                                        <span className="record-badge">
                                            {t("自定义·未核对", "Custom · Unverified")}
                                        </span>
                                    </p>
                                )}
                                <dl className="profile-details">
                                    <div>
                                        <dt>{t("资产类型", "Asset kind")}</dt>
                                        <dd>
                                            {asset.kind === "fiat"
                                                ? t("法币", "Fiat")
                                                : asset.kind === "native"
                                                  ? t("原生加密资产", "Native crypto")
                                                  : t("代币", "Token")}
                                        </dd>
                                    </div>
                                    <div>
                                        <dt>{t("小数位数", "Decimal places")}</dt>
                                        <dd>{asset.scale}</dd>
                                    </div>
                                    {asset.network && (
                                        <div>
                                            <dt>{t("网络", "Network")}</dt>
                                            <dd>{assetNetworkName(asset.network, locale)}</dd>
                                        </div>
                                    )}
                                    {asset.token_reference && (
                                        <div>
                                            <dt>{t("代币标识", "Token reference")}</dt>
                                            <dd>{asset.token_reference}</dd>
                                        </div>
                                    )}
                                </dl>
                                <div className="row-actions">
                                    <button
                                        disabled={busy || loading}
                                        onClick={() => void toggle(asset)}
                                    >
                                        {asset.enabled ? t("停用", "Disable") : t("启用", "Enable")}
                                    </button>
                                </div>
                            </article>
                        ))}
                    </div>
                    <p className="help-text">
                        {t(
                            "停用会阻止新的入账，历史记录与原币余额仍然可查。不会自动查询链上余额或执行交易。",
                            "Disabling prevents new postings while preserving history and original-asset balances. This does not query blockchain balances or execute trades.",
                        )}
                    </p>
                </>
            )}
        </section>
    );
}
