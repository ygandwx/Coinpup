import { useEffect, useRef, useState } from "react";
import type { Locale } from "./i18n";
import { listOperations } from "./ledger-api";
import type { Asset, OperationState } from "./ledger-api";
import { getConfirmation, listDraftDuplicates } from "./ocr-api";
import type { ConfirmationReceipt, DuplicateConfirmation } from "./ocr-api";
import type { Action } from "./ocr-entry";
import { parseAmount } from "./money";

export function OcrLinkPicker({
    ledger,
    locale,
    busy,
    onSave,
    onCancel,
    onError,
}: {
    ledger: string;
    locale: Locale;
    busy: boolean;
    onSave: (action: Action) => void;
    onCancel: () => void;
    onError: (error: unknown) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [rows, setRows] = useState<OperationState[]>([]),
        [page, setPage] = useState(0);
    const [id, setId] = useState(""),
        [refresh, setRefresh] = useState(0),
        [loading, setLoading] = useState(true);
    const failed = useRef(onError);
    useEffect(() => {
        failed.current = onError;
    }, [onError]);
    useEffect(() => {
        const abort = new AbortController();
        setLoading(true);
        setId("");
        setRows([]);
        void listOperations(
            ledger,
            { status: "active", order: "transaction_date", limit: 25, offset: page * 25 },
            abort.signal,
        )
            .then((items) => {
                if (!abort.signal.aborted) {
                    setRows(items);
                    setLoading(false);
                }
            })
            .catch((error) => {
                if (!abort.signal.aborted) {
                    failed.current(error);
                    setLoading(false);
                }
            });
        return () => abort.abort();
    }, [ledger, page, refresh]);
    const chosen = rows.find((row) => row.id === id);
    return (
        <section aria-label={t("关联已有流水", "Link existing entry")}>
            <p>
                {t(
                    "只添加票据证据，不重复入账。请选择已经存在的有效流水。",
                    "Attach evidence without posting again. Choose an existing active entry.",
                )}
            </p>
            <select
                aria-label={t("已有流水", "Existing entry")}
                value={id}
                disabled={busy || loading}
                onChange={(event) => setId(event.target.value)}
            >
                <option value="">{t("请选择", "Choose")}</option>
                {rows.map((row) => {
                    const post = row.latest_posting;
                    return (
                        <option key={row.id} value={row.id}>
                            {post.transaction_date} ·{" "}
                            {"amount" in post ? post.amount : post.source_amount}{" "}
                            {"asset_id" in post ? post.asset_id : post.source_asset_id} ·{" "}
                            {post.description || row.id}
                        </option>
                    );
                })}
            </select>
            <div className="ocr-actions">
                <button
                    disabled={busy || loading || page === 0}
                    onClick={() => setPage((n) => n - 1)}
                >
                    {t("上一页流水", "Previous entries")}
                </button>
                <button
                    disabled={busy || loading || rows.length < 25}
                    onClick={() => setPage((n) => n + 1)}
                >
                    {t("下一页流水", "Next entries")}
                </button>
                <button disabled={busy || loading} onClick={() => setRefresh((n) => n + 1)}>
                    {t("刷新流水", "Refresh entries")}
                </button>
                <button
                    disabled={busy || loading || !chosen}
                    onClick={() =>
                        chosen &&
                        onSave({
                            kind: "link",
                            operation_id: chosen.id,
                            expected_version: chosen.version,
                        })
                    }
                >
                    {t("保存关联草稿", "Save link draft")}
                </button>
                <button disabled={busy} onClick={onCancel}>
                    {t("取消关联选择", "Cancel link selection")}
                </button>
            </div>
        </section>
    );
}

type DuplicateState = { ready: boolean; acknowledged: boolean };
export function OcrDuplicateNotice({
    ledger,
    draft,
    version,
    action,
    assets,
    locale,
    locked,
    onChange,
    onError,
}: {
    ledger: string;
    draft: string;
    version: number;
    action: Action | null;
    assets: Asset[];
    locale: Locale;
    locked: boolean;
    onChange: (value: DuplicateState) => void;
    onError: (error: unknown) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [matches, setMatches] = useState<DuplicateConfirmation[]>([]),
        [ready, setReady] = useState(false),
        [ack, setAck] = useState(false);
    const [receipt, setReceipt] = useState<ConfirmationReceipt | null>(null),
        [refresh, setRefresh] = useState(0),
        [similar, setSimilar] = useState<OperationState[]>([]);
    const errorRef = useRef(onError);
    const inspection = useRef<AbortController | null>(null);
    useEffect(() => {
        errorRef.current = onError;
    }, [onError]);
    useEffect(() => {
        const abort = new AbortController();
        setReady(false);
        setAck(false);
        setMatches([]);
        setReceipt(null);
        void listDraftDuplicates(ledger, draft, abort.signal)
            .then((items) => {
                if (!abort.signal.aborted) {
                    setMatches(items);
                    setReady(true);
                }
            })
            .catch((error) => {
                if (!abort.signal.aborted) errorRef.current(error);
            });
        return () => {
            abort.abort();
            inspection.current?.abort();
        };
    }, [ledger, draft, version, refresh]);
    useEffect(() => {
        onChange({ ready: ready && (!matches.length || ack), acknowledged: ack });
    }, [ready, ack, matches.length, onChange]);
    const command = action && action.kind !== "link" ? action.command : null;
    const date = command?.transaction_date,
        asset = command && "asset_id" in command ? command.asset_id : "",
        amount = command && "amount" in command ? command.amount : "";
    const scale = assets.find((item) => item.asset_id === asset)?.scale;
    useEffect(() => {
        setSimilar([]);
        if (!date || !asset || !amount || scale === undefined) return;
        const abort = new AbortController();
        void listOperations(
            ledger,
            { status: "active", from_date: date, to_date: date, limit: 200 },
            abort.signal,
        )
            .then((rows) => {
                if (abort.signal.aborted) return;
                setSimilar(
                    rows.filter((row) => {
                        const post = row.latest_posting;
                        try {
                            return (
                                "asset_id" in post &&
                                "amount" in post &&
                                post.asset_id === asset &&
                                parseAmount(post.amount, scale) === parseAmount(amount, scale)
                            );
                        } catch {
                            return false;
                        }
                    }),
                );
            })
            .catch((error) => {
                if (!abort.signal.aborted) errorRef.current(error);
            });
        return () => abort.abort();
    }, [ledger, date, asset, amount, scale, refresh]);
    async function inspect(match: DuplicateConfirmation) {
        inspection.current?.abort();
        const abort = new AbortController();
        inspection.current = abort;
        setReceipt(null);
        try {
            const value = await getConfirmation(match.ledger_id, match.draft_id, abort.signal);
            if (!abort.signal.aborted) setReceipt(value);
        } catch (error) {
            if (!abort.signal.aborted) errorRef.current(error);
        }
    }
    return (
        <section aria-label={t("重复核对", "Duplicate review")}>
            {!ready && <p>{t("重复提示尚未就绪。", "Duplicate review is not ready.")}</p>}
            {!!matches.length && (
                <>
                    <p role="alert">
                        {t(
                            "同一原件行已有确认记录。请核对，不要重复入账。以下是历史确认，不代表记录当前状态。",
                            "This source row was already confirmed. Review it before posting again. These are historical confirmations, not current entry status.",
                        )}
                    </p>
                    <ul>
                        {matches.map((match) => (
                            <li key={match.draft_id}>
                                <button disabled={locked} onClick={() => void inspect(match)}>
                                    {t("查看历史确认", "Inspect prior confirmation")} ·{" "}
                                    {new Date(match.created_at).toLocaleString(locale)}
                                </button>
                            </li>
                        ))}
                    </ul>
                    {receipt && (
                        <p>
                            {t("历史金额", "Historical amount")}:{" "}
                            {"amount" in receipt.operation
                                ? receipt.operation.amount
                                : receipt.operation.source_amount}{" "}
                            {"asset_id" in receipt.operation
                                ? receipt.operation.asset_id
                                : receipt.operation.source_asset_id}
                        </p>
                    )}
                    <label>
                        <input
                            type="checkbox"
                            checked={ack}
                            disabled={locked}
                            onChange={(event) => setAck(event.target.checked)}
                        />
                        {t(
                            "已核对历史记录，仍明确确认本次额外操作",
                            "I reviewed the prior record and explicitly confirm this additional action",
                        )}
                    </label>
                </>
            )}
            {!!similar.length && (
                <p>
                    {t(
                        "存在同日期、同金额记录（仅检查当日首 200 条）。这只是相似提示，可关联已有记录或继续新建，不会自动合并。",
                        "Same-date, same-amount entries exist (first 200 entries checked). This is only a hint: link an existing entry or create a separate one. Nothing is merged automatically.",
                    )}
                </p>
            )}
            <button disabled={locked} onClick={() => setRefresh((n) => n + 1)}>
                {t("刷新重复提示", "Refresh duplicate hints")}
            </button>
        </section>
    );
}
