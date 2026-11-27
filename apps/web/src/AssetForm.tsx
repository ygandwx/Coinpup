import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import { legacyNativeIds } from "./asset-identity";
import { assetNetworkName } from "./asset-networks";
import type { AssetCreate, AssetKind, AssetPreset } from "./ledger-api";
import "./posting-form.css";

const FIXED_FIAT = new Set(["USD", "GBP", "EUR", "HKD", "CNY"]);
const copy = {
    zh: {
        title: "资产配置表单",
        kind: "资产类型",
        code: "资产代码",
        scale: "小数位数",
        network: "网络",
        reference: "代币标识",
        enabled: "启用",
        choose: "请选择",
        preset: "稳定币预设",
        prefill: "预填资料",
        loading: "正在读取资产预设…",
        retry: "重新读取预设",
        save: "保存",
        saving: "保存中…",
        cancel: "取消",
        kinds: { fiat: "法定货币", native: "原生加密资产", token: "代币" },
        fiatHelp:
            "使用 3 位大写代码；USD、GBP、EUR、HKD、CNY 固定为 2 位小数。其他币种请填写其实际精度。",
        nativeHelp: "选择原生加密资产后，将填入固定的网络和精度。",
        tokenHelp:
            "请核对所选网络、代币合约及小数位数。未匹配发行方预设的代币会标为「自定义·未核对」。",
        networkHelp: "以小写字母开头，可包含小写字母、数字、下划线和连字符，最多 64 字符。",
        referenceHelp:
            "保留原始大小写。以字母或数字开头，可包含字母、数字、点、下划线和连字符，最多 128 字符。",
        scaleHelp: "0 至 18 的整数。",
        errors: {
            code: "资产代码无效。法币使用 3 位大写字母，原生资产从列表中选择，代币使用 2 至 10 位大写字母或数字。",
            scale: "小数位数须为 0 至 18 的整数；内置法币固定为 2 位。",
            network: "请填写有效网络标识，使用小写字母开头的 1 至 64 个允许字符。",
            reference: "请填写有效代币标识，保留原始大小写并遵循所示字符范围。",
        },
    },
    en: {
        title: "Asset configuration form",
        kind: "Asset kind",
        code: "Asset code",
        scale: "Decimal places",
        network: "Network",
        reference: "Token reference",
        enabled: "Enabled",
        choose: "Choose",
        preset: "Stablecoin preset",
        prefill: "Fill in details",
        loading: "Loading asset presets…",
        retry: "Reload presets",
        save: "Save",
        saving: "Saving…",
        cancel: "Cancel",
        kinds: { fiat: "Fiat currency", native: "Native crypto asset", token: "Token" },
        fiatHelp:
            "Use a 3-letter uppercase code. USD, GBP, EUR, HKD and CNY use 2 decimal places. Enter the actual precision for other currencies.",
        nativeHelp: "The selected native asset determines the network and decimal places.",
        tokenHelp:
            "Check the network, token contract, and decimal places. Tokens outside the issuer presets are labeled Custom · Unverified.",
        networkHelp:
            "Start with a lowercase letter; use lowercase letters, digits, underscores or hyphens, up to 64 characters.",
        referenceHelp:
            "Preserve original case. Start with a letter or digit; use letters, digits, dots, underscores or hyphens, up to 128 characters.",
        scaleHelp: "An integer from 0 to 18.",
        errors: {
            code: "Enter 3 uppercase letters for fiat, pick a native asset, or use 2 to 10 uppercase letters or digits for a token.",
            scale: "Decimal places must be an integer from 0 to 18. Built-in fiat currencies require 2.",
            network:
                "Enter a valid network identifier using 1 to 64 allowed characters, starting with a lowercase letter.",
            reference:
                "Enter a valid token reference using the indicated characters and retain its original case.",
        },
    },
};

export type AssetFormProps = {
    locale: Locale;
    busy: boolean;
    fromPreset?: boolean;
    presets: readonly AssetPreset[] | undefined;
    presetError?: string;
    onRetryPresets: () => void;
    error?: string | null;
    onSubmit: (body: AssetCreate) => void;
    onCancel: () => void;
};

export function AssetForm({
    locale,
    busy,
    fromPreset,
    presets,
    presetError,
    onRetryPresets,
    error,
    onSubmit,
    onCancel,
}: AssetFormProps) {
    const t = copy[locale];
    const prefix = useId();
    const [presetId, setPresetId] = useState("");
    const [kind, setKind] = useState<AssetKind>("token");
    const [code, setCode] = useState("");
    const [scale, setScale] = useState("");
    const [network, setNetwork] = useState("");
    const [reference, setReference] = useState("");
    const [enabled, setEnabled] = useState(true);
    const [problem, setProblem] = useState<keyof typeof t.errors | null>(null);
    const nativePresets = presets?.filter((entry) => entry.kind === "native") ?? [];
    const tokenPresets = presets?.filter((entry) => entry.kind === "token") ?? [];
    const tokenNetworks = [...new Set(tokenPresets.map((entry) => entry.network))];
    const native = kind === "native" ? nativePresets.find((entry) => entry.code === code) : null;
    const fixedFiat = kind === "fiat" && FIXED_FIAT.has(code);
    const actualScale = native ? String(native.scale) : fixedFiat ? "2" : scale;
    const actualNetwork = native?.network ?? network;

    function changeKind(value: AssetKind) {
        setKind(value);
        setCode("");
        setScale("");
        setNetwork("");
        setReference("");
        setProblem(null);
    }

    function applyPreset() {
        const preset = tokenPresets.find((entry) => entry.asset_id === presetId);
        if (!preset?.token_reference) return;
        setKind("token");
        setCode(preset.code);
        setScale(String(preset.scale));
        setNetwork(preset.network);
        setReference(preset.token_reference);
        setProblem(null);
    }

    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (busy) return;
        const codeValid =
            kind === "fiat"
                ? /^[A-Z]{3}$/.test(code) && !legacyNativeIds.has(code)
                : kind === "native"
                  ? !!native
                  : /^[A-Z0-9]{2,10}$/.test(code);
        if (!codeValid) {
            setProblem("code");
            return;
        }
        if (!/^(?:[0-9]|1[0-8])$/.test(actualScale)) {
            setProblem("scale");
            return;
        }
        if (kind === "token" && !/^[a-z][a-z0-9_-]{0,63}$/.test(actualNetwork)) {
            setProblem("network");
            return;
        }
        if (kind === "token" && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(reference)) {
            setProblem("reference");
            return;
        }
        setProblem(null);
        onSubmit({
            kind,
            code,
            scale: Number(actualScale),
            network: kind === "fiat" ? null : actualNetwork,
            token_reference: kind === "token" ? reference : null,
            enabled,
        });
    }

    return (
        <form className="asset-form" onSubmit={submit} aria-label={t.title} aria-busy={busy}>
            {fromPreset && (
                <div className="form-grid">
                    <div className="field">
                        <label htmlFor={`${prefix}-preset`}>{t.preset}</label>
                        <select
                            id={`${prefix}-preset`}
                            value={presetId}
                            disabled={busy || !presets}
                            onChange={(event) => setPresetId(event.target.value)}
                        >
                            <option value="">{t.choose}</option>
                            {tokenPresets.map((preset) => (
                                <option key={preset.asset_id} value={preset.asset_id}>
                                    {preset.code} · {assetNetworkName(preset.network, locale)}
                                </option>
                            ))}
                        </select>
                    </div>
                    <div className="field preset-apply">
                        <button
                            type="button"
                            className="secondary-button"
                            disabled={busy || !presetId}
                            onClick={applyPreset}
                        >
                            {t.prefill}
                        </button>
                    </div>
                </div>
            )}
            {(fromPreset || kind === "native") &&
                !presets &&
                (presetError ? (
                    <div className="row-actions">
                        <p className="inline-error" role="alert">
                            {presetError}
                        </p>
                        <button className="secondary-button" type="button" onClick={onRetryPresets}>
                            {t.retry}
                        </button>
                    </div>
                ) : (
                    <p className="help-text" role="status">
                        {t.loading}
                    </p>
                ))}
            <div className="form-grid">
                <div className="field">
                    <label htmlFor={`${prefix}-kind`}>{t.kind}</label>
                    <select
                        id={`${prefix}-kind`}
                        value={kind}
                        disabled={busy}
                        onChange={(event) => changeKind(event.target.value as AssetKind)}
                    >
                        {(["fiat", "native", "token"] as AssetKind[]).map((value) => (
                            <option key={value} value={value}>
                                {t.kinds[value]}
                            </option>
                        ))}
                    </select>
                </div>
                <div className="field">
                    <label htmlFor={`${prefix}-code`}>{t.code}</label>
                    {kind !== "native" ? (
                        <input
                            id={`${prefix}-code`}
                            required
                            maxLength={kind === "fiat" ? 3 : 10}
                            value={code}
                            disabled={busy}
                            autoComplete="off"
                            autoCapitalize="characters"
                            spellCheck={false}
                            onChange={(event) => {
                                setCode(event.target.value.toUpperCase());
                                setProblem(null);
                            }}
                        />
                    ) : (
                        <select
                            id={`${prefix}-code`}
                            required
                            value={code}
                            disabled={busy || !presets}
                            onChange={(event) => {
                                setCode(event.target.value);
                                setProblem(null);
                            }}
                        >
                            <option value="">{t.choose}</option>
                            {nativePresets.map((entry) => (
                                <option key={entry.asset_id} value={entry.code}>
                                    {entry.code}
                                </option>
                            ))}
                        </select>
                    )}
                </div>
                <div className="field">
                    <label htmlFor={`${prefix}-scale`}>{t.scale}</label>
                    <input
                        id={`${prefix}-scale`}
                        type="text"
                        inputMode="numeric"
                        required
                        maxLength={2}
                        value={actualScale}
                        readOnly={!!native || fixedFiat}
                        disabled={busy}
                        onChange={(event) => {
                            setScale(event.target.value);
                            setProblem(null);
                        }}
                        aria-describedby={`${prefix}-scale-help`}
                    />
                    <p className="help-text" id={`${prefix}-scale-help`}>
                        {t.scaleHelp}
                    </p>
                </div>
                {kind !== "fiat" && (
                    <div className="field">
                        <label htmlFor={`${prefix}-network`}>{t.network}</label>
                        <input
                            id={`${prefix}-network`}
                            required
                            maxLength={64}
                            value={actualNetwork}
                            readOnly={kind === "native"}
                            disabled={busy}
                            spellCheck={false}
                            autoComplete="off"
                            autoCapitalize="none"
                            list={kind === "token" ? `${prefix}-networks` : undefined}
                            onChange={(event) => {
                                setNetwork(event.target.value);
                                setProblem(null);
                            }}
                            aria-describedby={`${prefix}-network-help`}
                        />
                        {kind === "token" && (
                            <datalist id={`${prefix}-networks`}>
                                {tokenNetworks.map((value) => (
                                    <option
                                        key={value}
                                        value={value}
                                        label={assetNetworkName(value, locale)}
                                    />
                                ))}
                            </datalist>
                        )}
                        <p className="help-text" id={`${prefix}-network-help`}>
                            {kind === "native" ? t.nativeHelp : t.networkHelp}
                        </p>
                    </div>
                )}
                {kind === "token" && (
                    <div className="field">
                        <label htmlFor={`${prefix}-reference`}>{t.reference}</label>
                        <input
                            id={`${prefix}-reference`}
                            required
                            maxLength={128}
                            value={reference}
                            disabled={busy}
                            spellCheck={false}
                            autoComplete="off"
                            autoCapitalize="none"
                            onChange={(event) => {
                                setReference(event.target.value);
                                setProblem(null);
                            }}
                            aria-describedby={`${prefix}-reference-help`}
                        />
                        <p className="help-text" id={`${prefix}-reference-help`}>
                            {t.referenceHelp}
                        </p>
                    </div>
                )}
            </div>
            {kind !== "native" && (
                <p className="help-text">{kind === "fiat" ? t.fiatHelp : t.tokenHelp}</p>
            )}
            <label className="asset-enabled">
                <input
                    type="checkbox"
                    checked={enabled}
                    disabled={busy}
                    onChange={(event) => setEnabled(event.target.checked)}
                />
                {t.enabled}
            </label>
            {(problem || error) && (
                <p className="inline-error" role="alert">
                    {problem ? t.errors[problem] : error}
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
