import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { AccountPicker } from "./AccountPicker";
import { ApiError } from "./api";
import { assetNetworkName } from "./asset-networks";
import { stripCommandText } from "./command-text";
import type { Locale } from "./i18n";
import { listAccounts, listCategories } from "./ledger-api";
import type { Account, Asset, Category, Entity, FeeCreate } from "./ledger-api";
import type { PlatformTransferCreate } from "./platform-transfers-api";

type Translate = (zh: string, en: string) => string;
type Choice = { id: string; name: string };
type Fee = FeeCreate & { id: string };
const emptyFee = () => ({
    id: crypto.randomUUID(),
    account_id: "",
    asset_id: "",
    amount: "",
    category_id: "",
});

async function pages<T>(read: (offset: number) => Promise<T[]>): Promise<T[]> {
    const rows: T[] = [];
    for (let offset = 0; offset <= 100000; offset += 100) {
        const page = await read(offset);
        rows.push(...page);
        if (page.length < 100) return rows;
    }
    throw new ApiError("server");
}

export function readTransferAccounts(
    ledger: string,
    includeArchived: boolean,
    signal: AbortSignal,
) {
    return pages((offset) =>
        listAccounts(ledger, { limit: 100, offset, include_archived: includeArchived }, signal),
    );
}

function ChoiceField({
    label,
    options,
    value,
    onChange,
    t,
}: {
    label: string;
    options: Choice[];
    value: string;
    onChange: (value: string) => void;
    t: Translate;
}) {
    const id = useId();
    return (
        <div className="field">
            <label htmlFor={id}>{label}</label>
            <select
                id={id}
                value={value}
                onChange={(event) => onChange(event.currentTarget.value)}
                required
            >
                <option value="">{t("请选择", "Choose")}</option>
                {options.map((option) => (
                    <option key={option.id} value={option.id}>
                        {option.name}
                    </option>
                ))}
            </select>
        </div>
    );
}

function AccountField({
    label,
    accounts,
    value,
    onChange,
    t,
    locale,
}: {
    label: string;
    accounts: Account[];
    value: string;
    onChange: (value: string) => void;
    t: Translate;
    locale: Locale;
}) {
    const id = useId();
    return (
        <div className="field">
            <label htmlFor={id}>{label}</label>
            <AccountPicker
                id={id}
                accounts={accounts}
                locale={locale}
                value={value}
                placeholder={t("选择账户", "Choose an account")}
                onChange={onChange}
            />
        </div>
    );
}

export function platformAssetLabel(asset: Asset, locale: Locale) {
    const label = asset.network
        ? assetNetworkName(asset.network, locale)
        : asset.kind === "custodial"
          ? locale === "zh"
              ? "托管"
              : "Custodial"
          : "";
    return label ? `${asset.code} · ${label}` : asset.code;
}

function amountField(t: Translate, label: string, value: string, change: (value: string) => void) {
    return (
        <label className="field">
            {label}
            <input
                inputMode="decimal"
                autoComplete="off"
                required
                maxLength={40}
                placeholder="0.00"
                value={value}
                onChange={(event) => change(event.currentTarget.value)}
                aria-description={t("使用十进制金额。", "Enter a decimal amount.")}
            />
        </label>
    );
}

function today() {
    const date = new Date();
    return [date.getFullYear(), date.getMonth() + 1, date.getDate()]
        .map((part, index) => String(part).padStart(index === 0 ? 4 : 2, "0"))
        .join("-");
}

export function PlatformTransferForm({
    t,
    locale,
    ledgerId,
    entities,
    assets,
    kind,
    locked,
    onSubmit,
    onDismiss,
    onError,
}: {
    t: Translate;
    locale: Locale;
    ledgerId: string;
    entities: Entity[];
    assets: Asset[];
    kind: PlatformTransferCreate["kind"];
    locked: boolean;
    onSubmit: (body: PlatformTransferCreate, from: Asset, to: Asset) => void;
    onDismiss: () => void;
    onError: (error: unknown) => void;
}) {
    const [destination, setDestination] = useState("");
    const [sourceData, setSourceData] = useState<{ accounts: Account[]; categories: Category[] }>();
    const [destinationData, setDestinationData] = useState<{
        ledger: string;
        accounts: Account[];
    }>();
    const [sourceFailed, setSourceFailed] = useState(false);
    const [destinationFailure, setDestinationFailure] = useState("");
    const [sourceAccount, setSourceAccount] = useState("");
    const [sourceAsset, setSourceAsset] = useState("");
    const [destinationAccount, setDestinationAccount] = useState("");
    const [destinationAsset, setDestinationAsset] = useState("");
    const [amount, setAmount] = useState("");
    const [fees, setFees] = useState<Fee[]>([]);
    const error = useRef(onError);
    error.current = onError;

    useEffect(() => {
        const abort = new AbortController();
        void Promise.all([
            readTransferAccounts(ledgerId, false, abort.signal),
            pages((offset) => listCategories(ledgerId, { limit: 100, offset }, abort.signal)),
        ]).then(
            ([accounts, categories]) => {
                if (!abort.signal.aborted) setSourceData({ accounts, categories });
            },
            (problem: unknown) => {
                if (!abort.signal.aborted) {
                    setSourceFailed(true);
                    error.current(problem);
                }
            },
        );
        return () => abort.abort();
    }, [ledgerId]);

    useEffect(() => {
        if (!destination || destination === ledgerId) return;
        const abort = new AbortController();
        void readTransferAccounts(destination, false, abort.signal).then(
            (accounts) => {
                if (!abort.signal.aborted) setDestinationData({ ledger: destination, accounts });
            },
            (problem: unknown) => {
                if (!abort.signal.aborted) {
                    setDestinationFailure(destination);
                    error.current(problem);
                }
            },
        );
        return () => abort.abort();
    }, [destination, ledgerId]);

    const sourceMode = kind === "deposit" ? "onchain" : "custodial";
    const destinationMode = kind === "deposit" ? "custodial" : "onchain";
    const sourceAccounts = sourceData?.accounts ?? [];
    const sameLedger = destination === ledgerId;
    const destinationAccounts = sameLedger
        ? sourceAccounts
        : destinationData?.ledger === destination
          ? destinationData.accounts
          : [];
    const expense = sourceData?.categories.filter((category) => category.kind === "expense") ?? [];
    const forAccount = (id: string, accounts: Account[]) => {
        const account = accounts.find((candidate) => candidate.id === id);
        return assets.filter(
            (asset) => asset.enabled && account?.asset_ids.includes(asset.asset_id),
        );
    };
    const eligibleAssets = (id: string, accounts: Account[], mode: string) =>
        forAccount(id, accounts).filter((asset) =>
            mode === "custodial"
                ? asset.kind === "custodial"
                : asset.kind === "native" || (asset.kind === "token" && asset.verified),
        );
    const sourceOptions = sourceAccounts.filter(
        (account) =>
            account.custody_mode === sourceMode &&
            eligibleAssets(account.id, sourceAccounts, sourceMode).length,
    );
    const from = eligibleAssets(sourceAccount, sourceAccounts, sourceMode);
    const source = from.find((asset) => asset.asset_id === sourceAsset);
    const toAssets = (id: string) =>
        eligibleAssets(id, destinationAccounts, destinationMode).filter(
            (asset) => asset.code === source?.code,
        );
    const destinationOptions = destinationAccounts.filter(
        (account) =>
            account.id !== sourceAccount &&
            account.custody_mode === destinationMode &&
            toAssets(account.id).length,
    );
    const to = toAssets(destinationAccount);
    const target = to.find((asset) => asset.asset_id === destinationAsset);
    const assetOptions = (options: Asset[]) =>
        options.map((asset) => ({ id: asset.asset_id, name: platformAssetLabel(asset, locale) }));

    function resetDestinationAccount() {
        setDestinationAccount("");
        setDestinationAsset("");
    }
    function changeFee(id: string, changes: Partial<FeeCreate>) {
        setFees((prior) => prior.map((fee) => (fee.id === id ? { ...fee, ...changes } : fee)));
    }
    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (locked || !source || !target) return;
        const form = new FormData(event.currentTarget);
        onSubmit(
            {
                kind,
                source_account_id: sourceAccount,
                source_asset_id: source.asset_id,
                destination_ledger_id: destination,
                destination_account_id: destinationAccount,
                destination_asset_id: target.asset_id,
                transaction_date: String(form.get("date")),
                amount,
                description: stripCommandText(String(form.get("description"))),
                fees: fees.map(({ account_id, asset_id, amount, category_id }) => ({
                    account_id,
                    asset_id,
                    amount,
                    category_id,
                })),
            },
            source,
            target,
        );
    }
    return (
        <form
            className="platform-transfer-form"
            onSubmit={submit}
            aria-label={
                kind === "deposit"
                    ? t("登记充值", "Record a deposit")
                    : t("登记提现", "Record a withdrawal")
            }
        >
            <fieldset disabled={locked}>
                <legend>
                    {kind === "deposit"
                        ? t("登记充值", "Record a deposit")
                        : t("登记提现", "Record a withdrawal")}
                </legend>
                <p className="help-text">
                    {t(
                        "同一币种按 1:1 入账。手续费单独记在付款账本。",
                        "Both sides record the same coin quantity. Fees are recorded separately in the sending ledger.",
                    )}
                </p>
                <div className="form-grid">
                    <AccountField
                        t={t}
                        locale={locale}
                        label={t("付款账户", "Sending account")}
                        accounts={sourceOptions}
                        value={sourceAccount}
                        onChange={(value) => {
                            setSourceAccount(value);
                            setSourceAsset("");
                            resetDestinationAccount();
                        }}
                    />
                    <ChoiceField
                        t={t}
                        label={t("付款资产", "Sending asset")}
                        options={assetOptions(from)}
                        value={sourceAsset}
                        onChange={(value) => {
                            setSourceAsset(value);
                            resetDestinationAccount();
                        }}
                    />
                    <ChoiceField
                        t={t}
                        label={t("收款账本", "Receiving ledger")}
                        options={entities
                            .filter((item) => !item.archived)
                            .map((item) => ({ id: item.ledger.id, name: item.name }))}
                        value={destination}
                        onChange={(value) => {
                            setDestination(value);
                            setDestinationFailure("");
                            resetDestinationAccount();
                        }}
                    />
                    <AccountField
                        t={t}
                        locale={locale}
                        label={t("收款账户", "Receiving account")}
                        accounts={destinationOptions}
                        value={destinationAccount}
                        onChange={(value) => {
                            setDestinationAccount(value);
                            setDestinationAsset("");
                        }}
                    />
                    <ChoiceField
                        t={t}
                        label={t("收款资产", "Receiving asset")}
                        options={assetOptions(to)}
                        value={destinationAsset}
                        onChange={setDestinationAsset}
                    />
                    <label className="field">
                        {t("交易日期", "Transaction date")}
                        <input name="date" type="date" required defaultValue={today()} />
                    </label>
                    {amountField(t, t("划转数量", "Transfer quantity"), amount, setAmount)}
                    <label className="field">
                        {t("备注", "Description")}
                        <input name="description" maxLength={2000} />
                    </label>
                </div>
                {!sourceData && !sourceFailed && (
                    <p role="status">{t("正在读取付款账户…", "Loading sending accounts…")}</p>
                )}
                {sourceData && !sourceOptions.length && (
                    <p className="help-text">
                        {t(
                            "此账本没有兼容的付款账户。请在账户设置中配置托管方式和启用的资产。",
                            "This ledger has no compatible sending account. Set a custody mode and enabled assets in Accounts.",
                        )}
                    </p>
                )}
                {source &&
                    destination &&
                    !sameLedger &&
                    destinationData?.ledger !== destination &&
                    destinationFailure !== destination && (
                        <p role="status">{t("正在读取收款账户…", "Loading receiving accounts…")}</p>
                    )}
                {source &&
                    (sameLedger || destinationData?.ledger === destination) &&
                    !destinationOptions.length && (
                        <p className="help-text">
                            {t(
                                "所选账本没有持有同一币种的兼容收款账户。",
                                "The selected ledger has no compatible receiving account for this coin.",
                            )}
                        </p>
                    )}
                {source && target && (
                    <p className="help-text">
                        {t(
                            `此组合最多支持 ${Math.min(source.scale, target.scale)} 位小数。`,
                            `This pair supports up to ${Math.min(source.scale, target.scale)} decimal places.`,
                        )}
                    </p>
                )}
                {fees.map((fee, index) => (
                    <fieldset key={fee.id}>
                        <legend>{t(`手续费 ${index + 1}`, `Fee ${index + 1}`)}</legend>
                        <div className="form-grid">
                            <AccountField
                                t={t}
                                locale={locale}
                                label={t("手续费账户", "Fee account")}
                                accounts={sourceAccounts}
                                value={fee.account_id}
                                onChange={(value) =>
                                    changeFee(fee.id, { account_id: value, asset_id: "" })
                                }
                            />
                            <ChoiceField
                                t={t}
                                label={t("手续费资产", "Fee asset")}
                                options={assetOptions(forAccount(fee.account_id, sourceAccounts))}
                                value={fee.asset_id}
                                onChange={(value) => changeFee(fee.id, { asset_id: value })}
                            />
                            {amountField(t, t("手续费数量", "Fee quantity"), fee.amount, (value) =>
                                changeFee(fee.id, { amount: value }),
                            )}
                            <ChoiceField
                                t={t}
                                label={t("手续费分类", "Fee category")}
                                options={expense}
                                value={fee.category_id}
                                onChange={(value) => changeFee(fee.id, { category_id: value })}
                            />
                        </div>
                        <button
                            type="button"
                            className="secondary-button"
                            onClick={() =>
                                setFees((prior) => prior.filter(({ id }) => fee.id !== id))
                            }
                        >
                            {t("移除手续费", "Remove fee")}
                        </button>
                    </fieldset>
                ))}
                <div className="platform-transfer-actions">
                    <button
                        type="button"
                        className="secondary-button"
                        disabled={!expense.length || fees.length >= 20}
                        onClick={() => setFees((prior) => [...prior, emptyFee()])}
                    >
                        {t("添加手续费", "Add fee")}
                    </button>
                    <button type="submit" className="primary-button" disabled={!source || !target}>
                        {t("登记充提", "Record transfer")}
                    </button>
                    <button type="button" className="secondary-button" onClick={onDismiss}>
                        {t("关闭表单", "Close form")}
                    </button>
                </div>
            </fieldset>
        </form>
    );
}
