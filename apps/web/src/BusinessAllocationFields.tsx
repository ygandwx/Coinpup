import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { businessError } from "./business-errors";
import { getBusinessConfirmation, listBusinessConfirmations } from "./business-confirmation-api";
import type { BusinessConfirmedSummary } from "./business-confirmation-api";
import { getDocumentSettlement } from "./business-settlement-api";
import type { InitialAllocationCreate } from "./business-settlement-api";
import type { AllocationPayment, AllocationSource } from "./settlement-fields";
import type { Asset } from "./ledger-api";
import type { Locale } from "./i18n";
import { formatAmount, parseAmount } from "./money";

export function BusinessAllocationFields({
    value,
    payment,
    sources,
    assets,
    locale,
    locked,
    onChange,
    onUnauthorized,
}: {
    value: InitialAllocationCreate;
    payment: AllocationPayment;
    sources: readonly AllocationSource[];
    assets: readonly Asset[];
    locale: Locale;
    locked: boolean;
    onChange: (group: InitialAllocationCreate, sources: readonly AllocationSource[]) => void;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const amount = (value: string, asset: string) => `${formatAmount(value, locale)} ${asset}`;
    const kind = payment.direction === "receipt" ? "invoice" : "bill";
    const context = JSON.stringify([payment.ledger_id, payment.party_id, kind, payment.asset_id]);
    const [offset, setOffset] = useState(0),
        [refresh, setRefresh] = useState(0);
    const key = JSON.stringify([context, offset, refresh]);
    const [page, setPage] = useState<{
        key: string;
        rows: BusinessConfirmedSummary[];
    } | null>(null);
    const [error, setError] = useState<{ key: string; error: unknown } | null>(null);
    const [detail, setDetail] = useState<{
        key: string;
        sources: AllocationSource[];
        name: string;
    } | null>(null);
    const [reading, setReading] = useState<string | null>(null);
    const callback = useRef(onUnauthorized);
    callback.current = onUnauthorized;
    const detailAbort = useRef<AbortController | null>(null);
    const rows = page?.key === key ? page.rows : null;
    const eligible =
        rows?.filter((row) => row.party_id === payment.party_id && row.document_kind === kind) ??
        null;
    const visible = detail?.key === key ? detail : null;
    useEffect(() => () => detailAbort.current?.abort(), [key]);
    useEffect(() => {
        const abort = new AbortController();
        listBusinessConfirmations(payment.ledger_id, offset, abort.signal)
            .then((rows) => {
                if (abort.signal.aborted) return;
                if (rows.some((row) => row.ledger_id !== payment.ledger_id))
                    throw new ApiError("server", 200, "invalid_response");
                setPage({ key, rows });
            })
            .catch((caught) => {
                if (abort.signal.aborted) return;
                if (caught instanceof ApiError && caught.status === 401) callback.current();
                else setError({ key, error: caught });
            });
        return () => abort.abort();
    }, [payment.ledger_id, offset, key]);
    async function read(id: string) {
        if (locked) return;
        detailAbort.current?.abort();
        const abort = new AbortController();
        detailAbort.current = abort;
        setReading(key);
        setDetail(null);
        setError(null);
        try {
            const [sealed, balance] = await Promise.all([
                getBusinessConfirmation(payment.ledger_id, id, abort.signal),
                getDocumentSettlement(payment.ledger_id, id, abort.signal),
            ]);
            if (abort.signal.aborted) return;
            const doc = sealed.document;
            if (
                doc.id !== id ||
                doc.ledger_id !== payment.ledger_id ||
                doc.party_id !== payment.party_id ||
                doc.document_kind !== kind ||
                balance.id !== id ||
                balance.ledger_id !== payment.ledger_id ||
                balance.party_id !== doc.party_id ||
                balance.asset_id !== doc.asset_id
            )
                throw new ApiError("server", 200, "invalid_response");
            const scale = assets.find((row) => row.asset_id === doc.asset_id)?.scale;
            if (scale === undefined) throw new ApiError("server", 422, "asset_unavailable");
            const choices: AllocationSource[] = [];
            for (const line of balance.lines) {
                const original = doc.lines.find((row) => row.id === line.id);
                if (
                    !original ||
                    original.total_amount !== line.total_amount ||
                    original.project_id !== line.project_id
                )
                    throw new ApiError("server", 200, "invalid_response");
                if (parseAmount(line.outstanding_amount, scale) <= 0n) continue;
                choices.push({
                    document_id: id,
                    document_line_id: line.id,
                    ledger_id: doc.ledger_id,
                    party_id: doc.party_id,
                    document_kind: doc.document_kind,
                    issue_date: doc.issue_date,
                    asset_id: doc.asset_id,
                    outstanding_amount: line.outstanding_amount,
                    description: original.description,
                    project_id: line.project_id,
                });
            }
            setDetail({
                key,
                sources: choices,
                name: `${String(doc.party_snapshot.name ?? doc.party_id)} · ${doc.issue_date}`,
            });
        } catch (caught) {
            if (abort.signal.aborted) return;
            if (caught instanceof ApiError && caught.status === 401) callback.current();
            else setError({ key, error: caught });
        } finally {
            if (detailAbort.current === abort) setReading(null);
        }
    }
    function add(source: AllocationSource) {
        if (
            locked ||
            value.lines.length >= 200 ||
            value.lines.some((line) => line.document_line_id === source.document_line_id)
        )
            return;
        onChange(
            {
                ...value,
                lines: [
                    ...value.lines,
                    {
                        document_id: source.document_id,
                        document_line_id: source.document_line_id,
                        payment_amount: "",
                        document_amount: "",
                    },
                ],
            },
            [...sources, source],
        );
    }
    return (
        <fieldset disabled={locked} className="draft-fields">
            <legend>{t("明确分摊至单据行", "Allocate to document lines")}</legend>
            <p className="help-text">
                {t(
                    "可选部分金额，最多200行。日期不得早于付款日和各单据开票日，且期间须开放。余额仅为读取时预览，提交时重新校验。",
                    "Apply partial amounts to up to 200 lines. The date must be on or after the payment and every issue date, in an open period. Balances are previews and are checked again when submitted.",
                )}
            </p>
            <div className="field">
                <label>
                    {t("分摊生效日", "Allocation date")}
                    <input
                        type="date"
                        required
                        value={value.allocation_date}
                        onChange={(event) =>
                            onChange({ ...value, allocation_date: event.target.value }, sources)
                        }
                    />
                </label>
            </div>
            <ol className="confirmation-lines">
                {value.lines.map((line, index) => {
                    const source = sources[index],
                        same = source?.asset_id === payment.asset_id;
                    return (
                        <li key={line.document_line_id}>
                            <strong>{source?.description ?? line.document_line_id}</strong>
                            <p>
                                {source?.issue_date} ·{" "}
                                {source && amount(source.outstanding_amount, source.asset_id)}{" "}
                                {t("未结", "outstanding")}
                            </p>
                            <div className="field">
                                <label>
                                    {same
                                        ? t("分摊金额", "Allocation amount")
                                        : t("付款侧数量", "Payment quantity")}{" "}
                                    ({payment.asset_id})
                                    <input
                                        type="text"
                                        inputMode="decimal"
                                        required
                                        maxLength={40}
                                        value={line.payment_amount}
                                        onChange={(event) =>
                                            onChange(
                                                {
                                                    ...value,
                                                    lines: value.lines.map((row, position) =>
                                                        position === index
                                                            ? {
                                                                  ...row,
                                                                  payment_amount:
                                                                      event.target.value,
                                                                  ...(same
                                                                      ? {
                                                                            document_amount:
                                                                                event.target.value,
                                                                        }
                                                                      : {}),
                                                              }
                                                            : row,
                                                    ),
                                                },
                                                sources,
                                            )
                                        }
                                    />
                                </label>
                            </div>
                            {!same && (
                                <div className="field">
                                    <label>
                                        {t("单据侧数量", "Document quantity")} ({source?.asset_id})
                                        <input
                                            type="text"
                                            inputMode="decimal"
                                            required
                                            maxLength={40}
                                            value={line.document_amount}
                                            onChange={(event) =>
                                                onChange(
                                                    {
                                                        ...value,
                                                        lines: value.lines.map((row, position) =>
                                                            position === index
                                                                ? {
                                                                      ...row,
                                                                      document_amount:
                                                                          event.target.value,
                                                                  }
                                                                : row,
                                                        ),
                                                    },
                                                    sources,
                                                )
                                            }
                                        />
                                    </label>
                                </div>
                            )}
                            <button
                                type="button"
                                onClick={() =>
                                    onChange(
                                        {
                                            ...value,
                                            lines: value.lines.filter(
                                                (_, position) => position !== index,
                                            ),
                                        },
                                        sources.filter((_, position) => position !== index),
                                    )
                                }
                            >
                                {t("移除此项分摊", "Remove this allocation")}
                            </button>
                        </li>
                    );
                })}
            </ol>
            <h3>{t("选择已确认单据", "Choose confirmed documents")}</h3>
            {error?.key === key && <p role="alert">{businessError(error.error, locale)}</p>}
            {reading === key && (
                <p role="status">{t("正在读取明细…", "Loading document lines…")}</p>
            )}
            {visible ? (
                <>
                    <p>{visible.name}</p>
                    <ul className="master-records">
                        {visible.sources.map((source) => (
                            <li key={source.document_line_id}>
                                <strong>{source.description}</strong>
                                <span>{amount(source.outstanding_amount, source.asset_id)}</span>
                                <button
                                    type="button"
                                    disabled={
                                        value.lines.length >= 200 ||
                                        value.lines.some(
                                            (row) =>
                                                row.document_line_id === source.document_line_id,
                                        )
                                    }
                                    onClick={() => add(source)}
                                >
                                    {t("选择此明细", "Select this line")}
                                </button>
                            </li>
                        ))}
                    </ul>
                    {visible.sources.length === 0 && (
                        <p>{t("没有正的未结明细。", "No positive outstanding lines.")}</p>
                    )}
                    <button type="button" onClick={() => setDetail(null)}>
                        {t("返回单据列表", "Back to document list")}
                    </button>
                </>
            ) : (
                <>
                    {!rows && error?.key !== key && (
                        <p role="status">{t("正在读取…", "Loading…")}</p>
                    )}
                    <ul className="master-records">
                        {eligible?.map((row) => (
                            <li key={row.id}>
                                <strong>
                                    {String(row.party_snapshot.name ?? row.party_id)} ·{" "}
                                    {row.issue_date}
                                </strong>
                                <span>{amount(row.total_amount, row.asset_id)}</span>
                                <button type="button" onClick={() => read(row.id)}>
                                    {t("选择单据明细", "Choose document lines")}
                                </button>
                            </li>
                        ))}
                    </ul>
                    {eligible?.length === 0 && (
                        <p>
                            {t(
                                "此页没有匹配往来和方向的单据；可继续翻页。",
                                "No matching party and direction on this page; other pages may contain matches.",
                            )}
                        </p>
                    )}
                    <div className="form-actions allocation-pagination">
                        <button
                            type="button"
                            disabled={!rows || offset === 0}
                            onClick={() => setOffset((value) => value - 25)}
                        >
                            {t("上一页单据", "Previous documents")}
                        </button>
                        <span>
                            {t("页", "Page")} {offset / 25 + 1}
                        </span>
                        <button
                            type="button"
                            disabled={rows?.length !== 25 || offset >= 100000}
                            onClick={() => setOffset((value) => value + 25)}
                        >
                            {t("下一页单据", "Next documents")}
                        </button>
                        <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                            {t("刷新单据", "Refresh documents")}
                        </button>
                    </div>
                </>
            )}
        </fieldset>
    );
}
