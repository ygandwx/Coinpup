import { useEffect, useState } from "react";
import { ApiError } from "./api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";
import { listControlBalances } from "./ledger-api";
import type { Asset, ControlBalance } from "./ledger-api";
import { amountFromMinorUnits, formatAmount, parseAmount } from "./money";

const LABELS: Record<ControlBalance["system_key"], [string, string]> = {
    "receivable.customer": ["客户应收", "Customer receivables"],
    "payable.supplier": ["供应商应付", "Supplier payables"],
    "intercompany.receivable": ["应收往来", "Due from entities"],
    "intercompany.payable": ["应付往来", "Due to entities"],
    "advance.received": ["客户预收", "Customer advances"],
    "advance.paid": ["供应商预付", "Supplier prepayments"],
};
const PAYABLE = new Set(["payable.supplier", "intercompany.payable", "advance.received"]);

export function ControlBalancesPanel({
    ledgerId,
    locale,
    assets,
    onUnauthorized,
}: {
    ledgerId: string;
    locale: Locale;
    assets: Asset[];
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [kind, setKind] = useState<ControlBalance["system_key"] | "">("");
    const [offset, setOffset] = useState(0);
    const [refresh, setRefresh] = useState(0);
    const key = JSON.stringify([ledgerId, kind, offset, refresh]);
    const [result, setResult] = useState<{ key: string; rows: ControlBalance[] } | null>(null);
    const [failure, setFailure] = useState<{ key: string; error: unknown } | null>(null);
    const rows = result?.key === key ? result.rows : null;
    const error = failure?.key === key ? failure.error : null;
    useEffect(() => {
        const controller = new AbortController();
        listControlBalances(
            ledgerId,
            { system_key: kind || undefined, limit: 25, offset },
            controller.signal,
        )
            .then((items) => {
                if (!controller.signal.aborted) setResult({ key, rows: items });
            })
            .catch((cause) => {
                if (controller.signal.aborted) return;
                if (cause instanceof ApiError && cause.status === 401) onUnauthorized();
                else setFailure({ key, error: cause });
            });
        return () => controller.abort();
    }, [ledgerId, kind, offset, key, onUnauthorized]);
    return (
        <section
            className="business-panel control-balances"
            aria-label={t("往来余额", "Control balances")}
        >
            <div className="section-heading">
                <h2>{t("往来余额", "Control balances")}</h2>
                <button type="button" onClick={() => setRefresh((value) => value + 1)}>
                    {t("刷新", "Refresh")}
                </button>
            </div>
            <p className="help-text">
                {t(
                    "各方向、资产、往来单位和单据分开显示，不合计为可用资金。应付与预收按应付方向展示，原始分录符号同时保留。",
                    "Directions, assets, parties and documents remain separate from available funds. Payables and received advances use the payable direction; original signed amounts remain visible.",
                )}
            </p>
            <label className="field">
                {t("往来方向", "Control direction")}
                <select
                    value={kind}
                    onChange={(event) => {
                        setKind(event.target.value as typeof kind);
                        setOffset(0);
                    }}
                >
                    <option value="">{t("全部方向", "All directions")}</option>
                    {Object.entries(LABELS).map(([value, labels]) => (
                        <option key={value} value={value}>
                            {t(...labels)}
                        </option>
                    ))}
                </select>
            </label>
            {error != null && <p role="alert">{businessError(error, locale)}</p>}
            {!rows && !error && (
                <p role="status">{t("正在读取往来余额…", "Loading control balances…")}</p>
            )}
            {rows?.length === 0 && (
                <p>{t("此页没有往来余额。", "No control balances on this page.")}</p>
            )}
            <div className="control-grid">
                {rows?.map((row) => {
                    const asset = assets.find((item) => item.asset_id === row.asset_id);
                    const directionAmount =
                        asset && PAYABLE.has(row.system_key)
                            ? amountFromMinorUnits(
                                  -parseAmount(row.amount, asset.scale),
                                  asset.scale,
                              )
                            : row.amount;
                    return (
                        <article
                            className="account-card"
                            data-testid="control-balance"
                            key={JSON.stringify(row)}
                        >
                            <h3>{t(...LABELS[row.system_key])}</h3>
                            <strong className="money-quantity" data-testid="control-amount">
                                {formatAmount(directionAmount, locale)} {row.asset_id}
                            </strong>
                            <p className="help-text">
                                {t("原始分录余额", "Original signed balance")}: {row.amount}{" "}
                                {row.asset_id}
                            </p>
                            {(row.account_archived || !row.asset_enabled || !row.link_enabled) && (
                                <p className="record-badge">
                                    {t(
                                        "已归档或停用 · 历史余额",
                                        "Archived or disabled · historical balance",
                                    )}
                                </p>
                            )}
                            <dl>
                                <dt>{t("往来单位", "Party")}</dt>
                                <dd>{row.party_id ?? "—"}</dd>
                                <dt>{t("对方主体", "Counterparty entity")}</dt>
                                <dd>{row.counterparty_entity_id ?? "—"}</dd>
                                <dt>{t("单据", "Document")}</dt>
                                <dd>{row.document_id ?? "—"}</dd>
                                <dt>{t("单据行", "Document line")}</dt>
                                <dd>{row.document_line_id ?? "—"}</dd>
                            </dl>
                        </article>
                    );
                })}
            </div>
            <div className="form-actions">
                <button
                    type="button"
                    disabled={offset === 0}
                    onClick={() => setOffset((value) => Math.max(0, value - 25))}
                >
                    {t("上一页", "Previous page")}
                </button>
                <span>
                    {t("页", "Page")} {offset / 25 + 1}
                </span>
                <button
                    type="button"
                    disabled={rows?.length !== 25 || offset >= 100000}
                    onClick={() => setOffset((value) => value + 25)}
                >
                    {t("下一页", "Next page")}
                </button>
            </div>
        </section>
    );
}
