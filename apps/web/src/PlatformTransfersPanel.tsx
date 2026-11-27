import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { AccountName } from "./AccountIcon";
import { AssetIcon } from "./AssetIcon";
import { DisplayAmount } from "./DisplayAmount";
import { LoadingSkeleton } from "./LoadingSkeleton";
import {
    PlatformTransferForm,
    platformAssetLabel,
    readTransferAccounts,
} from "./PlatformTransferForm";
import { ApiError } from "./api";
import type { Session } from "./api";
import { businessError } from "./business-errors";
import { stripCommandText, validBusinessReason } from "./command-text";
import type { Locale } from "./i18n";
import type { Account, Asset, Entity } from "./ledger-api";
import { AmountError } from "./money";
import type { PendingPlatformTransfer } from "./pending-platform-transfer";
import { getPlatformTransfer, listPlatformTransfers } from "./platform-transfers-api";
import type { PlatformTransferDetail, PlatformTransferCreate } from "./platform-transfers-api";
import "./platform-transfers.css";

type Translate = (zh: string, en: string) => string;
const pageSize = 25;

function validCancellationReason(reason: string): boolean {
    return validBusinessReason(reason) && [...stripCommandText(reason)].length <= 1000;
}

function message(error: unknown, locale: Locale, t: Translate) {
    if (error instanceof AmountError)
        return t(
            "请输入两边资产均能精确表示的十进制数量；手续费须符合各自资产精度。",
            "Enter a decimal quantity exact on both sides. Each fee must also fit its own asset precision.",
        );
    if (error instanceof ApiError && error.code === "amount_positive")
        return t("划转及手续费数量须大于零。", "Transfer and fee quantities must be positive.");
    if (error instanceof ApiError && error.code === "idempotency_conflict")
        return t(
            "请求键已用于不同的充提操作。",
            "This request key belongs to a different transfer.",
        );
    return businessError(error, locale);
}

function direction(kind: PlatformTransferCreate["kind"], t: Translate) {
    return kind === "deposit" ? t("充值", "Deposit") : t("提现", "Withdrawal");
}

function Quantity({
    amount,
    assetId,
    assets,
    locale,
    expense = false,
}: {
    amount: string;
    assetId: string;
    assets: Asset[];
    locale: Locale;
    expense?: boolean;
}) {
    const asset = assets.find((item) => item.asset_id === assetId);
    return (
        <span className="platform-transfer-quantity">
            {asset && <AssetIcon asset={asset} compact />}
            <span>
                <DisplayAmount
                    amount={amount}
                    assetId={asset?.code ?? assetId}
                    locale={locale}
                    expense={expense}
                />
                {asset && <small>{platformAssetLabel(asset, locale)}</small>}
            </span>
        </span>
    );
}

export function PlatformTransfersPanel({
    locale,
    session,
    ledgerId,
    entities,
    assets,
    controller,
    refresh,
    onChanged,
    onUnauthorized,
}: {
    locale: Locale;
    session: Session;
    ledgerId: string;
    entities: Entity[];
    assets: Asset[];
    controller: PendingPlatformTransfer;
    refresh: number;
    onChanged: () => void;
    onUnauthorized: () => void;
}) {
    const t: Translate = (zh, en) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const locked = !["idle", "confirmed", "rejected", "conflict"].includes(pending.status);
    const recoveryLedger = pending.plan?.ledgerId;
    const scope = locked && recoveryLedger ? recoveryLedger : ledgerId;
    const [page, setPage] = useState(0);
    const [list, setList] = useState<{ key: string; rows: PlatformTransferDetail[] }>();
    const [detail, setDetail] = useState<{ key: string; row: PlatformTransferDetail }>();
    const [accountData, setAccountData] = useState<{
        key: string;
        accounts: Record<string, Account[]>;
    }>();
    const [selected, setSelected] = useState("");
    const [form, setForm] = useState<PlatformTransferCreate["kind"] | null>(null);
    const [reason, setReason] = useState("");
    const [cancelling, setCancelling] = useState(false);
    const [failure, setFailure] = useState<unknown>(null);
    const [readFailure, setReadFailure] = useState<{ key: string; error: unknown }>();
    const [detailFailure, setDetailFailure] = useState<{ key: string; error: unknown }>();
    const [reload, setReload] = useState(0);
    const callbacks = useRef({ onChanged, onUnauthorized });
    callbacks.current = { onChanged, onUnauthorized };

    const listKey = JSON.stringify([scope, refresh, reload, page]);
    const detailKey = JSON.stringify([scope, refresh, reload, selected]);
    const rows = list?.key === listKey ? list.rows : null;
    const current = detail?.key === detailKey ? detail.row : null;
    const ledger = (id: string) => entities.find((entity) => entity.ledger.id === id);
    const sourceEntity = ledger(scope);
    const scopeReadError = readFailure?.key === listKey ? readFailure.error : null;
    const detailReadError = detailFailure?.key === detailKey ? detailFailure.error : null;

    function receiveError(error: unknown) {
        if (error instanceof ApiError && error.kind === "unauthorized") {
            callbacks.current.onUnauthorized();
            return;
        }
        setFailure(error);
    }

    useEffect(() => {
        const abort = new AbortController();
        void listPlatformTransfers(scope, pageSize, page * pageSize, abort.signal).then(
            (loaded) => {
                if (!abort.signal.aborted) setList({ key: listKey, rows: loaded });
            },
            (error: unknown) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.kind === "unauthorized")
                    callbacks.current.onUnauthorized();
                else setReadFailure({ key: listKey, error });
            },
        );
        return () => abort.abort();
    }, [scope, page, listKey]);

    useEffect(() => {
        if (!selected) return;
        const abort = new AbortController();
        void getPlatformTransfer(scope, selected, abort.signal).then(
            (row) => {
                if (!abort.signal.aborted) setDetail({ key: detailKey, row });
            },
            (error: unknown) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.kind === "unauthorized")
                    callbacks.current.onUnauthorized();
                else setDetailFailure({ key: detailKey, error });
            },
        );
        return () => abort.abort();
    }, [scope, selected, detailKey]);

    const sourceLedger = current?.source_ledger_id;
    const destinationLedger = current?.destination_ledger_id;
    useEffect(() => {
        if (!sourceLedger || !destinationLedger) return;
        const abort = new AbortController();
        const ledgers = [...new Set([sourceLedger, destinationLedger])];
        void Promise.all(ledgers.map((id) => readTransferAccounts(id, true, abort.signal))).then(
            (loaded) => {
                if (!abort.signal.aborted)
                    setAccountData({
                        key: detailKey,
                        accounts: Object.fromEntries(
                            ledgers.map((id, index) => [id, loaded[index]!]),
                        ),
                    });
            },
            (error: unknown) => {
                if (
                    !abort.signal.aborted &&
                    error instanceof ApiError &&
                    error.kind === "unauthorized"
                )
                    callbacks.current.onUnauthorized();
            },
        );
        return () => abort.abort();
    }, [sourceLedger, destinationLedger, detailKey]);

    function select(id: string) {
        setSelected(id);
        setCancelling(false);
        setReason("");
    }

    function accountName(ledgerId: string, id: string) {
        const account =
            accountData?.key === detailKey
                ? accountData.accounts[ledgerId]?.find((value) => value.id === id)
                : undefined;
        return account ? <AccountName account={account} /> : id;
    }

    function onResult() {
        const { status, record, error } = controller.getSnapshot();
        if (status === "auth-required") callbacks.current.onUnauthorized();
        if (status === "confirmed" && record) {
            setForm(null);
            select(record.id);
            setPage(0);
            setFailure(null);
            callbacks.current.onChanged();
        } else if (status === "rejected" && error?.status === 409) {
            setCancelling(false);
            setReload((value) => value + 1);
        }
    }

    function create(body: PlatformTransferCreate, from: Asset, to: Asset) {
        setFailure(null);
        void controller.create(session, scope, body, from, to, assets).then(onResult, receiveError);
    }

    function cancel(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (
            !current ||
            locked ||
            current.source_ledger_id !== scope ||
            !validCancellationReason(reason)
        )
            return;
        setFailure(null);
        void controller
            .cancel(session, current, stripCommandText(reason))
            .then(onResult, receiveError);
    }

    function journalRows(items: PlatformTransferDetail["operations"]) {
        return (
            <ul className="platform-transfer-receipts">
                {items.map((item) => (
                    <li key={item.journal_id}>
                        {ledger(item.ledger_id)?.name ?? item.ledger_id} ·{" "}
                        <code>{item.journal_id}</code>
                    </li>
                ))}
            </ul>
        );
    }

    return (
        <section
            className="business-panel platform-transfers"
            aria-label={t("平台充提", "Platform transfers")}
        >
            <div className="section-heading">
                <div>
                    <p className="help-text">
                        {t(
                            "在链上钱包和托管账户之间登记同一币种的划转。",
                            "Record transfers of the same coin between an on-chain wallet and a custodial account.",
                        )}
                    </p>
                </div>
                {!locked && !sourceEntity?.archived && !form && (
                    <div className="platform-transfer-actions">
                        {(["deposit", "withdrawal"] as const).map((kind) => (
                            <button
                                key={kind}
                                type="button"
                                className="secondary-button"
                                onClick={() => {
                                    controller.dismiss();
                                    setFailure(null);
                                    setForm(kind);
                                }}
                            >
                                {kind === "deposit"
                                    ? t("登记充值", "Record a deposit")
                                    : t("登记提现", "Record a withdrawal")}
                            </button>
                        ))}
                    </div>
                )}
            </div>
            {pending.status === "submitting" && (
                <p role="status">{t("正在提交…", "Submitting…")}</p>
            )}
            {pending.status === "unknown" && (
                <div className="pending-command" role="alert">
                    <p>
                        {t(
                            "充提结果待确认。请使用原请求和原键核对。",
                            "The transfer result is unconfirmed. Retry with the original request and key.",
                        )}
                    </p>
                    <button
                        className="secondary-button"
                        type="button"
                        onClick={() => void controller.retry(session).then(onResult, receiveError)}
                    >
                        {t("重试原请求", "Retry original request")}
                    </button>
                </div>
            )}
            {pending.status === "confirmed" && (
                <p role="status">
                    {pending.record && "reversals" in pending.record
                        ? t("整组冲销已核对。", "The entire transfer reversal is confirmed.")
                        : t("充提凭证已核对。", "The transfer postings are confirmed.")}
                </p>
            )}
            {(failure != null || pending.status === "rejected") && (
                <p className="inline-error" role="alert">
                    {message(failure ?? pending.error, locale, t)}
                </p>
            )}
            {form && (
                <PlatformTransferForm
                    key={form}
                    t={t}
                    locale={locale}
                    ledgerId={scope}
                    entities={entities}
                    assets={assets}
                    kind={form}
                    locked={locked}
                    onSubmit={create}
                    onDismiss={() => {
                        setForm(null);
                        setFailure(null);
                        controller.dismiss();
                    }}
                    onError={receiveError}
                />
            )}
            <h3>{t("充提记录", "Transfer history")}</h3>
            {scopeReadError != null && <p role="alert">{message(scopeReadError, locale, t)}</p>}
            {!rows && scopeReadError == null && (
                <LoadingSkeleton label={t("正在读取充提记录…", "Loading transfers…")} />
            )}
            {rows?.length === 0 && (
                <p className="help-text">
                    {t("此账本还没有充提记录。", "This ledger has no platform transfers yet.")}
                </p>
            )}
            {!!rows?.length && (
                <ul className="platform-transfer-list">
                    {rows.map((row) => (
                        <li key={row.id}>
                            <div>
                                <strong>{direction(row.kind, t)}</strong> ·{" "}
                                <time dateTime={row.transaction_date}>{row.transaction_date}</time>
                                <small>{`${ledger(row.source_ledger_id)?.name ?? row.source_ledger_id} → ${ledger(row.destination_ledger_id)?.name ?? row.destination_ledger_id}`}</small>
                            </div>
                            <Quantity
                                amount={row.source_amount}
                                assetId={row.source_asset_id}
                                assets={assets}
                                locale={locale}
                            />
                            <span>
                                {row.status === "cancelled"
                                    ? t("已取消", "Cancelled")
                                    : t("已入账", "Posted")}
                            </span>
                            <button
                                type="button"
                                className="secondary-button"
                                aria-expanded={selected === row.id}
                                disabled={locked}
                                onClick={() => {
                                    controller.dismiss();
                                    select(selected === row.id ? "" : row.id);
                                }}
                            >
                                {selected === row.id
                                    ? t("关闭详情", "Close details")
                                    : t("查看详情", "View details")}
                            </button>
                        </li>
                    ))}
                </ul>
            )}
            {(page > 0 || rows?.length === pageSize) && (
                <div className="platform-transfer-actions">
                    <button
                        type="button"
                        className="secondary-button"
                        disabled={locked || page === 0}
                        onClick={() => {
                            setPage((value) => value - 1);
                            select("");
                        }}
                    >
                        {t("上一页", "Previous page")}
                    </button>
                    <button
                        type="button"
                        className="secondary-button"
                        disabled={locked || rows?.length !== pageSize || page * pageSize >= 100000}
                        onClick={() => {
                            setPage((value) => value + 1);
                            select("");
                        }}
                    >
                        {t("下一页", "Next page")}
                    </button>
                </div>
            )}
            {selected && detailReadError != null && (
                <p role="alert">{message(detailReadError, locale, t)}</p>
            )}
            {selected && !current && detailReadError == null && (
                <LoadingSkeleton label={t("正在读取充提详情…", "Loading transfer details…")} />
            )}
            {current && (
                <section
                    className="platform-transfer-detail"
                    aria-label={t("充提详情", "Transfer details")}
                >
                    <h3>{t("充提详情", "Transfer details")}</h3>
                    <div className="form-grid">
                        <div>
                            <h4>
                                {t("付款", "Sent")} ·{" "}
                                {ledger(current.source_ledger_id)?.name ?? current.source_ledger_id}
                            </h4>
                            <div>
                                {accountName(current.source_ledger_id, current.source_account_id)}
                            </div>
                            <Quantity
                                amount={current.source_amount}
                                assetId={current.source_asset_id}
                                assets={assets}
                                locale={locale}
                                expense
                            />
                        </div>
                        <div>
                            <h4>
                                {t("收款", "Received")} ·{" "}
                                {ledger(current.destination_ledger_id)?.name ??
                                    current.destination_ledger_id}
                            </h4>
                            <div>
                                {accountName(
                                    current.destination_ledger_id,
                                    current.destination_account_id,
                                )}
                            </div>
                            <Quantity
                                amount={current.destination_amount}
                                assetId={current.destination_asset_id}
                                assets={assets}
                                locale={locale}
                            />
                        </div>
                    </div>
                    {current.description && <p>{current.description}</p>}
                    {!!current.fees.length && (
                        <>
                            <h4>{t("手续费", "Fees")}</h4>
                            <ul className="platform-transfer-receipts">
                                {current.fees.map((fee, index) => (
                                    <li key={`${fee.account_id}:${fee.asset_id}:${index}`}>
                                        {accountName(current.source_ledger_id, fee.account_id)}
                                        {" · "}
                                        <Quantity
                                            amount={fee.amount}
                                            assetId={fee.asset_id}
                                            assets={assets}
                                            locale={locale}
                                            expense
                                        />
                                    </li>
                                ))}
                            </ul>
                        </>
                    )}
                    <h4>{t("原始凭证", "Original journals")}</h4>
                    {journalRows(current.operations)}
                    {current.cancellation && (
                        <>
                            <h4>
                                {t(
                                    "取消原因与冲销凭证",
                                    "Cancellation reason and reversal journals",
                                )}
                            </h4>
                            <p>{current.cancellation.reason}</p>
                            {journalRows(current.cancellation.reversals)}
                        </>
                    )}
                    {current.status === "active" && current.source_ledger_id !== scope && (
                        <p className="help-text">
                            {t(
                                "如需取消整笔充提，请切换到原付款账本。",
                                "Switch to the original sending ledger to cancel the entire transfer.",
                            )}
                        </p>
                    )}
                    {current.status === "active" &&
                        current.source_ledger_id === scope &&
                        !cancelling && (
                            <button
                                type="button"
                                className="secondary-button"
                                disabled={locked}
                                onClick={() => {
                                    controller.dismiss();
                                    setFailure(null);
                                    setCancelling(true);
                                }}
                            >
                                {t("取消整笔充提", "Cancel entire transfer")}
                            </button>
                        )}
                    {cancelling && (
                        <form
                            onSubmit={cancel}
                            aria-label={t("取消整笔充提", "Cancel entire transfer")}
                        >
                            <fieldset disabled={locked}>
                                <label className="field">
                                    {t("取消原因", "Cancellation reason")}
                                    <textarea
                                        required
                                        maxLength={2000}
                                        value={reason}
                                        onChange={(event) => setReason(event.currentTarget.value)}
                                    />
                                </label>
                                <div className="platform-transfer-actions">
                                    <button
                                        type="submit"
                                        className="primary-button"
                                        disabled={!validCancellationReason(reason)}
                                    >
                                        {t("确认整组冲销", "Confirm entire reversal")}
                                    </button>
                                    <button
                                        type="button"
                                        className="secondary-button"
                                        onClick={() => setCancelling(false)}
                                    >
                                        {t("返回详情", "Back to details")}
                                    </button>
                                </div>
                            </fieldset>
                        </form>
                    )}
                </section>
            )}
        </section>
    );
}
