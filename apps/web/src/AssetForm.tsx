import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { AssetCreate, AssetKind } from "./ledger-api";
import "./posting-form.css";

// Keep these explicit built-in identities aligned with ledger/assets.py.
// A stablecoin's network, reference and precision are never inferred from code.
const NATIVE = {
    BTC: { network: "bitcoin", scale: 8 },
    ETH: { network: "ethereum", scale: 18 },
    XMR: { network: "monero", scale: 12 },
} as const;
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
        save: "保存",
        saving: "保存中…",
        cancel: "取消",
        kinds: { fiat: "法定货币", native: "原生加密资产", token: "稳定币代币" },
        intro: "资产身份和精度保存后固定。请核对资料，后续可启用或停用资产。",
        fiatHelp:
            "使用 3 位大写代码；USD、GBP、EUR、HKD、CNY 固定为 2 位小数。其他币种请填写其实际精度。",
        nativeHelp: "BTC、ETH、XMR 的网络和精度已固定。",
        tokenHelp:
            "明确填写所选网络、代币合约或发行标识、实际小数位数。USDT／USDC 的名称本身不能确定这些资料。",
        networkHelp: "以小写字母开头，可包含小写字母、数字、下划线和连字符，最多 64 字符。",
        referenceHelp:
            "保留原始大小写。以字母或数字开头，可包含字母、数字、点、下划线和连字符，最多 128 字符。",
        scaleHelp: "0 至 18 的整数，不会自动推测稳定币精度。",
        errors: {
            code: "请填写该资产类型支持的代码。法币为 3 位大写字母，原生资产为 BTC／ETH／XMR，代币为 USDT／USDC。",
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
        save: "Save",
        saving: "Saving…",
        cancel: "Cancel",
        kinds: { fiat: "Fiat currency", native: "Native crypto asset", token: "Stablecoin token" },
        intro: "Asset identity and precision are fixed after saving. Check these details; you can enable or disable the asset later.",
        fiatHelp:
            "Use a 3-letter uppercase code. USD, GBP, EUR, HKD and CNY use 2 decimal places. Enter the actual precision for other currencies.",
        nativeHelp: "BTC, ETH and XMR have fixed networks and precision.",
        tokenHelp:
            "Enter the specific network, token contract or issuer reference, and actual decimal places. The name USDT or USDC does not establish these details.",
        networkHelp:
            "Start with a lowercase letter; use lowercase letters, digits, underscores or hyphens, up to 64 characters.",
        referenceHelp:
            "Preserve original case. Start with a letter or digit; use letters, digits, dots, underscores or hyphens, up to 128 characters.",
        scaleHelp: "An integer from 0 to 18. Stablecoin precision is not inferred.",
        errors: {
            code: "Use a code supported by this asset kind: 3 uppercase letters for fiat, BTC/ETH/XMR for native assets, or USDT/USDC for tokens.",
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
    error?: string | null;
    onSubmit: (body: AssetCreate) => void;
    onCancel: () => void;
};

export function AssetForm({ locale, busy, error, onSubmit, onCancel }: AssetFormProps) {
    const t = copy[locale];
    const prefix = useId();
    const [kind, setKind] = useState<AssetKind>("token");
    const [code, setCode] = useState("");
    const [scale, setScale] = useState("");
    const [network, setNetwork] = useState("");
    const [reference, setReference] = useState("");
    const [enabled, setEnabled] = useState(true);
    const [problem, setProblem] = useState<keyof typeof t.errors | null>(null);
    const native =
        kind === "native" && code in NATIVE ? NATIVE[code as keyof typeof NATIVE] : undefined;
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

    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (busy) return;
        const codeValid =
            kind === "fiat"
                ? /^[A-Z]{3}$/.test(code) && !(code in NATIVE)
                : kind === "native"
                  ? code in NATIVE
                  : code === "USDT" || code === "USDC";
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
            <p className="help-text asset-form-intro">{t.intro}</p>
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
                    {kind === "fiat" ? (
                        <input
                            id={`${prefix}-code`}
                            required
                            maxLength={3}
                            value={code}
                            disabled={busy}
                            autoComplete="off"
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
                            disabled={busy}
                            onChange={(event) => {
                                setCode(event.target.value);
                                setProblem(null);
                            }}
                        >
                            <option value="">{t.choose}</option>
                            {(kind === "native" ? Object.keys(NATIVE) : ["USDT", "USDC"]).map(
                                (value) => (
                                    <option key={value} value={value}>
                                        {value}
                                    </option>
                                ),
                            )}
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
                            onChange={(event) => {
                                setNetwork(event.target.value);
                                setProblem(null);
                            }}
                            aria-describedby={`${prefix}-network-help`}
                        />
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
            <p className="help-text">
                {kind === "fiat" ? t.fiatHelp : kind === "native" ? t.nativeHelp : t.tokenHelp}
            </p>
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
