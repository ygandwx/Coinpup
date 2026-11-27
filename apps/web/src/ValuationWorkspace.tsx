import { EntityMark } from "./EntityMark";
import { cloneElement, useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent, ReactElement } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import { businessError } from "./business-errors";
import { validDraftDate } from "./draft-fields";
import type { Locale } from "./i18n";
import type { Asset, Entity } from "./ledger-api";
import { AmountError, formatReportAmount } from "./money";
import type { PendingPrice } from "./pending-price";
import { readManualPrices, readMvpReport } from "./valuation-api";
import type { ManualPrice, ManualPriceInput, MvpReport, ReportQuery } from "./valuation-api";
import "./valuation.css";

const displayAssets = ["USD", "GBP", "EUR", "HKD", "CNY"] as const;
type DisplayAsset = ManualPriceInput["display_asset_id"];
type View = MvpReport["view"];
type PriceBasis = "exact" | NonNullable<ReportQuery["price_mode"]>;
type AutomaticPrice = NonNullable<MvpReport["rows"][number]["automatic_price"]>;
type Translate = (zh: string, en: string) => string;
type AssetName = (assetId: string) => string;
type ViewProps = { t: Translate; locale: Locale; onUnauthorized: () => void; assetName: AssetName };
const pageSize = 25;

function SimpleField({
    label,
    children,
}: {
    label: string;
    children: ReactElement<{ id?: string }>;
}) {
    const id = useId();
    return (
        <div className="field">
            <label htmlFor={id}>{label}</label>
            {cloneElement(children, { id })}
        </div>
    );
}

function targets() {
    return displayAssets.map((id) => <option key={id}>{id}</option>);
}

function useRead<Q, R>(
    read: (query: Q, signal: AbortSignal) => Promise<R>,
    query: Q | null,
    onUnauthorized: () => void,
    refresh?: ManualPrice | null,
) {
    const [outcome, setOutcome] = useState<
        | { query: Q; refresh: ManualPrice | null | undefined; page: R }
        | { query: Q; refresh: ManualPrice | null | undefined; error: unknown }
        | null
    >(null);
    const unauthorized = useRef(onUnauthorized);
    unauthorized.current = onUnauthorized;
    useEffect(() => {
        if (!query) return;
        const abort = new AbortController();
        void read(query, abort.signal).then(
            (page) => {
                if (!abort.signal.aborted) setOutcome({ query, refresh, page });
            },
            (error: unknown) => {
                if (abort.signal.aborted) return;
                if (error instanceof ApiError && error.kind === "unauthorized")
                    unauthorized.current();
                else setOutcome({ query, refresh, error });
            },
        );
        return () => abort.abort();
    }, [query, read, refresh]);
    return outcome?.query === query && outcome.refresh === refresh ? outcome : null;
}

function PageButtons({
    t,
    offset,
    hasMore,
    onPage,
}: {
    t: Translate;
    offset: number;
    hasMore: boolean;
    onPage: (offset: number) => void;
}) {
    return (
        (offset > 0 || hasMore) && (
            <div className="valuation-actions">
                <button
                    className="secondary-button"
                    type="button"
                    disabled={!offset}
                    onClick={() => onPage(offset - pageSize)}
                >
                    {t("上一页", "Previous page")}
                </button>
                <button
                    className="secondary-button"
                    type="button"
                    disabled={!hasMore || offset + pageSize > 100000}
                    onClick={() => onPage(offset + pageSize)}
                >
                    {t("下一页", "Next page")}
                </button>
            </div>
        )
    );
}

function AutomaticPriceEvidence({
    quote,
    t,
    assetName,
}: {
    quote: AutomaticPrice;
    t: Translate;
    assetName: AssetName;
}) {
    return (
        <>
            <dt>{t("使用的自动报价", "Cached automatic price applied")}</dt>
            <dd>
                {quote.price} {quote.display_asset_id} / {assetName(quote.asset_id)} ·{" "}
                {quote.price_date} · {quote.source}
            </dd>
            {quote.observed_at && (
                <>
                    <dt>{t("报价时点（UTC）", "Observed time (UTC)")}</dt>
                    <dd>
                        <time dateTime={quote.observed_at}>{quote.observed_at}</time>
                    </dd>
                </>
            )}
            <dt>{t("获取时间（UTC）", "Fetched time (UTC)")}</dt>
            <dd>
                <time dateTime={quote.fetched_at}>{quote.fetched_at}</time>
            </dd>
            <dt>{t("报价记录", "Price observation")}</dt>
            <dd>
                <code>{quote.id}</code>
            </dd>
        </>
    );
}

function ReportPanel({
    entities,
    initialLedger,
    t,
    locale,
    assetName,
    onUnauthorized,
    savedPrice,
}: ViewProps & { entities: Entity[]; initialLedger?: string; savedPrice: ManualPrice | null }) {
    const [selection, setSelection] = useState<string[] | null>(null);
    const ledgerIds = selection ?? (initialLedger ? [initialLedger] : []);
    const [view, setView] = useState<View>("balances");
    const [display, setDisplay] = useState<DisplayAsset | "">("");
    const [priceBasis, setPriceBasis] = useState<PriceBasis>("exact");
    const [priceDate, setPriceDate] = useState("");
    const [query, setQuery] = useState<ReportQuery | null>(null);
    const currentQuote =
        savedPrice &&
        query?.display_asset_id === savedPrice.display_asset_id &&
        (!query.price_date || query.price_date === savedPrice.price_date)
            ? savedPrice
            : null;
    const result = useRead(readMvpReport, query, onUnauthorized, currentQuote);
    const page = result && "page" in result ? result.page : null;
    const names = new Map(entities.map(({ ledger, name }) => [ledger.id, name]));
    const balances = view === "balances";
    const historicalBalance = balances && Boolean(display) && priceBasis === "historical";
    const displayAmount = (amount: string, id: string) =>
        `${formatReportAmount(amount, locale)} ${assetName(id)}`;
    const reportName = t("原币汇总与估值", "Original-currency totals and valuation");

    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        const fields = new FormData(event.currentTarget);
        const value = (name: string) => String(fields.get(name) ?? "");
        const from_date = balances ? "" : value("from_date");
        const to_date = value("to_date");
        const prices =
            priceBasis === "exact" ? { price_date: priceDate } : { price_mode: priceBasis };
        const display_asset_id = display;
        if (selection === null) setSelection(ledgerIds);
        setQuery({
            ledger_ids: [...ledgerIds],
            view,
            ...(from_date && { from_date }),
            ...(to_date && { to_date }),
            ...(display_asset_id && { display_asset_id, ...prices }),
            offset: 0,
            limit: pageSize,
        });
    }

    return (
        <section className="business-panel valuation-panel" aria-label={reportName}>
            <h2>{reportName}</h2>
            <form onSubmit={submit} onChange={() => setQuery(null)}>
                <fieldset className="valuation-ledgers">
                    <legend>{t("纳入汇总的主体", "Entities to include")}</legend>
                    {entities.map((entity) => (
                        <label className="valuation-entity" key={entity.id}>
                            <input
                                type="checkbox"
                                checked={ledgerIds.includes(entity.ledger.id)}
                                onChange={({ target: { checked } }) =>
                                    setSelection(
                                        checked
                                            ? [...ledgerIds, entity.ledger.id]
                                            : ledgerIds.filter((id) => id !== entity.ledger.id),
                                    )
                                }
                            />
                            <EntityMark entity={entity} compact />
                            <span>
                                {entity.name}
                                {entity.archived && t("（已归档）", " (archived)")}
                            </span>
                        </label>
                    ))}
                </fieldset>
                <div className="form-grid">
                    <SimpleField label={t("报表口径", "Report view")}>
                        <select
                            value={view}
                            onChange={(event) => setView(event.target.value as View)}
                        >
                            <option value="balances">{t("账户余额", "Account balances")}</option>
                            <option value="cash">{t("实际资金流", "Cash movements")}</option>
                            <option value="operating">
                                {t("经营收支", "Operating income and expenses")}
                            </option>
                        </select>
                    </SimpleField>
                    {!balances && (
                        <SimpleField label={t("开始日期", "From date")}>
                            <input name="from_date" type="date" />
                        </SimpleField>
                    )}
                    <SimpleField
                        label={
                            historicalBalance
                                ? t("余额截止日", "Balance as of")
                                : balances
                                  ? t("余额截止日（可选）", "Balance as of (optional)")
                                  : t("结束日期", "Through date")
                        }
                    >
                        <input name="to_date" type="date" required={historicalBalance} />
                    </SimpleField>
                    <SimpleField label={t("折算为", "Value in")}>
                        <select
                            value={display}
                            onChange={(event) =>
                                setDisplay(event.target.value as DisplayAsset | "")
                            }
                        >
                            <option value="">{t("仅原币", "Original currencies only")}</option>
                            {targets()}
                        </select>
                    </SimpleField>
                    {display && (
                        <SimpleField label={t("报价口径", "Price basis")}>
                            <select
                                value={priceBasis}
                                onChange={(event) =>
                                    setPriceBasis(event.target.value as PriceBasis)
                                }
                            >
                                <option value="exact">{t("指定日期", "Specified date")}</option>
                                <option value="latest">
                                    {t("最新缓存报价", "Latest cached prices")}
                                </option>
                                <option value="historical">
                                    {t("历史报价", "Historical prices")}
                                </option>
                            </select>
                        </SimpleField>
                    )}
                    {display && priceBasis === "exact" && (
                        <SimpleField label={t("指定报价日期", "Price date")}>
                            <input
                                name="price_date"
                                type="date"
                                value={priceDate}
                                onChange={(event) => setPriceDate(event.target.value)}
                                required
                            />
                        </SimpleField>
                    )}
                </div>
                <button
                    className="primary-button"
                    type="submit"
                    disabled={!ledgerIds.length || ledgerIds.length > 100}
                >
                    {t("生成汇总", "Run report")}
                </button>
            </form>
            {view === "cash" && (
                <p className="help-text">
                    {t(
                        "只有纳入偿还的双方时，才从总额中抵销已关联的内部偿还。",
                        "Linked internal repayments are excluded from totals only when both ledgers are included.",
                    )}
                </p>
            )}
            {query && !result && <p role="status">{t("正在读取汇总…", "Loading report…")}</p>}
            {result && "error" in result && (
                <p role="alert">{reportError(result.error, t, locale)}</p>
            )}
            {page && (
                <>
                    <h3>{t("原币总额", "Totals by original currency")}</h3>
                    {page.originals.length === 0 && (
                        <p>{t("所选范围没有记录。", "No records in this selection.")}</p>
                    )}
                    <ul className="valuation-items">
                        {page.originals.map((item) => (
                            <li key={item.asset_id} data-testid="valuation-original">
                                <strong>{displayAmount(item.amount, item.asset_id)}</strong>
                                {item.inflow !== null && (
                                    <p>
                                        {view === "operating"
                                            ? t("收入", "Income")
                                            : t("流入", "Inflow")}{" "}
                                        {displayAmount(item.inflow, item.asset_id)}
                                    </p>
                                )}
                                {item.outflow !== null && (
                                    <p>
                                        {view === "operating"
                                            ? t("费用", "Expenses")
                                            : t("流出", "Outflow")}{" "}
                                        {displayAmount(item.outflow, item.asset_id)}
                                    </p>
                                )}
                            </li>
                        ))}
                    </ul>
                    {page.valuation && (
                        <section aria-label={t("折算结果", "Valuation result")}>
                            <h3>
                                {t("折算结果", "Valuation result")} ·{" "}
                                {page.valuation.price_date ??
                                    (query?.price_mode === "latest"
                                        ? t("最新缓存报价", "Latest cached prices")
                                        : t("历史报价", "Historical prices"))}
                            </h3>
                            {page.valuation.amount === null ? (
                                <p role="status">
                                    {t(
                                        "缺少价格，无法折算完整总额。",
                                        "The valuation total is unavailable because prices are missing.",
                                    )}{" "}
                                    {page.valuation.missing_asset_ids.map(assetName).join(" · ")} (
                                    {page.valuation.missing_rows})
                                </p>
                            ) : (
                                <p>
                                    <strong>
                                        {displayAmount(
                                            page.valuation.amount,
                                            page.valuation.display_asset_id,
                                        )}
                                    </strong>
                                </p>
                            )}
                        </section>
                    )}
                    <h3>
                        {t("来源明细", "Source details")} ({page.total_rows})
                    </h3>
                    <ul className="valuation-details">
                        {page.rows.map((row) => (
                            <li key={JSON.stringify([row.ledger_id, row.source_id, row.asset_id])}>
                                <details>
                                    <summary>
                                        <strong>{displayAmount(row.amount, row.asset_id)}</strong>
                                        {" · "}
                                        {names.get(row.ledger_id) ?? row.ledger_id}
                                        {row.date && <> · {row.date}</>}
                                        {row.repayment_group_id && (
                                            <>
                                                {" "}
                                                ·{" "}
                                                {row.excluded_repayment_group
                                                    ? t(
                                                          "已抵销内部偿还",
                                                          "Internal repayment excluded",
                                                      )
                                                    : t("内部偿还", "Internal repayment")}
                                            </>
                                        )}
                                    </summary>
                                    <dl>
                                        {row.description && (
                                            <>
                                                <dt>{t("说明", "Description")}</dt>
                                                <dd>{row.description}</dd>
                                            </>
                                        )}
                                        <dt>{t("来源记录", "Source record")}</dt>
                                        <dd>
                                            <code>{row.source_id}</code>
                                        </dd>
                                        {row.account_id && (
                                            <>
                                                <dt>{t("账户", "Account")}</dt>
                                                <dd>
                                                    <code>{row.account_id}</code>
                                                </dd>
                                            </>
                                        )}
                                        {row.category_id && (
                                            <>
                                                <dt>{t("分类", "Category")}</dt>
                                                <dd>
                                                    <code>{row.category_id}</code>
                                                </dd>
                                            </>
                                        )}
                                        {row.operation_id && (
                                            <>
                                                <dt>{t("入账凭证", "Posting")}</dt>
                                                <dd>
                                                    <code>{row.operation_id}</code>
                                                </dd>
                                            </>
                                        )}
                                        {row.repayment_group_id && (
                                            <>
                                                <dt>
                                                    {t(
                                                        "内部偿还关联",
                                                        "Internal repayment reference",
                                                    )}
                                                </dt>
                                                <dd>
                                                    <code>{row.repayment_group_id}</code>
                                                </dd>
                                            </>
                                        )}
                                        {row.price && (
                                            <>
                                                <dt>
                                                    {t("使用的手动价格", "Manual price applied")}
                                                </dt>
                                                <dd>
                                                    {row.price.price} {row.price.display_asset_id} /{" "}
                                                    {assetName(row.price.asset_id)} ·{" "}
                                                    {row.price.price_date} · {row.price.source}
                                                </dd>
                                            </>
                                        )}
                                        {row.automatic_price && (
                                            <AutomaticPriceEvidence
                                                quote={row.automatic_price}
                                                t={t}
                                                assetName={assetName}
                                            />
                                        )}
                                        {row.valuation_state === "missing" && (
                                            <>
                                                <dt>{t("折算结果", "Valuation result")}</dt>
                                                <dd>
                                                    {query?.price_mode === "latest"
                                                        ? t(
                                                              "缺少可用的缓存报价",
                                                              "No available cached price",
                                                          )
                                                        : query?.price_mode === "historical"
                                                          ? t(
                                                                "缺少该记录对应日期的报价",
                                                                "No price for this record’s date",
                                                            )
                                                          : t(
                                                                "缺少所选日期的报价",
                                                                "No price for the selected date",
                                                            )}
                                                </dd>
                                            </>
                                        )}
                                        {row.valuation_state === "identity" && (
                                            <>
                                                <dt>{t("使用的价格", "Price applied")}</dt>
                                                <dd>
                                                    1 {row.asset_id} / {row.asset_id}
                                                </dd>
                                            </>
                                        )}
                                    </dl>
                                </details>
                            </li>
                        ))}
                    </ul>
                    {query && (
                        <PageButtons
                            t={t}
                            offset={page.offset}
                            hasMore={page.offset + page.limit < page.total_rows}
                            onPage={(offset) => setQuery({ ...query, offset })}
                        />
                    )}
                </>
            )}
        </section>
    );
}

function reportError(error: unknown, t: Translate, locale: Locale) {
    if (error instanceof ApiError && error.kind === "network")
        return t("读取失败，请重试。", "The report could not be loaded. Try again.");
    if (error instanceof ApiError && error.status === 422)
        return t(
            "请核对主体、日期范围和报价口径。",
            "Check the selected entities, date range and price basis.",
        );
    return businessError(error, locale);
}

type PriceTableQuery = Parameters<typeof readManualPrices>[0];

function PricePanel({
    t,
    locale,
    onUnauthorized,
    assetName,
    assets,
    session,
    controller,
}: ViewProps & { assets: Asset[]; session: Session; controller: PendingPrice }) {
    const [scope, setScope] = useState<PriceTableQuery | null>(null);
    const [priceDate, setPriceDate] = useState("");
    const [target, setTarget] = useState<DisplayAsset>("USD");
    const [editing, setEditing] = useState<ManualPrice | null>(null);
    const [assetId, setAssetId] = useState("");
    const [price, setPrice] = useState("");
    const [source, setSource] = useState("");
    const [localError, setLocalError] = useState<unknown>(null);
    const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const locked = !["idle", "rejected", "confirmed", "conflict"].includes(state.status);
    const result = useRead(readManualPrices, scope, onUnauthorized);
    const page = result && "page" in result ? result.page : null;
    const title = t("手动价格表", "Manual prices");

    function edit(row: ManualPrice | null) {
        if (row) {
            setPriceDate(row.price_date);
            setTarget(row.display_asset_id);
        }
        setEditing(row);
        setAssetId(row?.asset_id ?? "");
        setPrice(row?.price ?? "");
        setSource(row?.source ?? "");
    }
    useEffect(() => {
        if (state.status === "auth-required") onUnauthorized();
    }, [state.status, onUnauthorized]);
    useEffect(() => {
        if (state.status !== "confirmed" || !state.record) return;
        setScope({
            price_date: state.record.price_date,
            display_asset_id: state.record.display_asset_id,
            limit: pageSize,
            offset: 0,
        });
        setPriceDate(state.record.price_date);
        setTarget(state.record.display_asset_id);
        setEditing(state.record);
        setAssetId(state.record.asset_id);
        setPrice(state.record.price);
        setSource(state.record.source);
    }, [state.status, state.record]);

    function selectDate(value: string) {
        setPriceDate(value);
        setScope(null);
        edit(null);
        controller.dismiss();
    }
    function selectTarget(value: DisplayAsset) {
        setTarget(value);
        setScope(null);
        edit(null);
        controller.dismiss();
    }
    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (locked || !validDraftDate(priceDate) || !assetId || !source.trim()) return;
        setLocalError(null);
        void controller
            .start(session, {
                asset_id: assetId,
                display_asset_id: target,
                price_date: priceDate,
                price,
                source: source.trim(),
                expected_version: editing?.version ?? 0,
            })
            .catch(setLocalError);
    }
    const canSave =
        !locked &&
        state.status !== "conflict" &&
        validDraftDate(priceDate) &&
        assetId &&
        assetId !== target &&
        source.trim() &&
        price;
    return (
        <section className="business-panel valuation-panel" aria-label={title}>
            <h2>{title}</h2>
            <div className="form-grid">
                <SimpleField label={t("报价日期", "Quote date")}>
                    <input
                        type="date"
                        value={priceDate}
                        onChange={(event) => selectDate(event.target.value)}
                        disabled={locked || Boolean(editing)}
                    />
                </SimpleField>
                <SimpleField label={t("计价币种", "Quote currency")}>
                    <select
                        value={target}
                        onChange={(event) => selectTarget(event.target.value as DisplayAsset)}
                        disabled={locked || Boolean(editing)}
                    >
                        {targets()}
                    </select>
                </SimpleField>
            </div>
            <button
                type="button"
                className="secondary-button"
                disabled={locked || !validDraftDate(priceDate)}
                onClick={() =>
                    setScope({
                        price_date: priceDate,
                        display_asset_id: target,
                        limit: pageSize,
                        offset: 0,
                    })
                }
            >
                {t("查看该日价格", "Show prices for this date")}
            </button>
            {scope && !result && <p role="status">{t("正在读取价格…", "Loading prices…")}</p>}
            {result && "error" in result && (
                <p role="alert">{reportError(result.error, t, locale)}</p>
            )}
            {page && (
                <>
                    {page.items.length === 0 && (
                        <p>
                            {t(
                                "该日期和计价币种暂无手动价格。",
                                "No manual prices for this date and currency.",
                            )}
                        </p>
                    )}
                    <ul className="valuation-items">
                        {page.items.map((item) => (
                            <li key={item.id}>
                                <strong>{assetName(item.asset_id)}</strong>
                                <p>
                                    {item.price} {item.display_asset_id} · {item.source}
                                </p>
                                <button
                                    type="button"
                                    className="secondary-button"
                                    disabled={locked}
                                    onClick={() => {
                                        controller.dismiss();
                                        edit(item);
                                    }}
                                >
                                    {t("编辑价格", "Edit price")}{" "}
                                    <span className="sr-only">{assetName(item.asset_id)}</span>
                                </button>
                            </li>
                        ))}
                    </ul>
                    {scope && (
                        <PageButtons
                            t={t}
                            offset={page.offset}
                            hasMore={page.has_more}
                            onPage={(offset) => setScope({ ...scope, offset })}
                        />
                    )}
                </>
            )}
            <form
                aria-label={
                    editing
                        ? t("编辑手动价格", "Edit manual price")
                        : t("添加手动价格", "Add manual price")
                }
                onSubmit={submit}
            >
                <div className="form-grid">
                    <SimpleField label={t("报价资产", "Priced asset")}>
                        <select
                            required
                            value={assetId}
                            onChange={(event) => setAssetId(event.target.value)}
                            disabled={locked || Boolean(editing)}
                        >
                            <option value="">{t("选择资产", "Select an asset")}</option>
                            {assets
                                .filter((asset) => asset.asset_id !== target)
                                .map((asset) => (
                                    <option key={asset.asset_id} value={asset.asset_id}>
                                        {assetName(asset.asset_id)}
                                    </option>
                                ))}
                        </select>
                    </SimpleField>
                    <SimpleField label={t("每单位价格", "Price per unit")}>
                        <input
                            value={price}
                            onChange={(event) => setPrice(event.target.value)}
                            required
                            inputMode="decimal"
                            maxLength={39}
                            disabled={locked}
                        />
                    </SimpleField>
                    <SimpleField label={t("价格来源", "Price source")}>
                        <input
                            value={source}
                            onChange={(event) => setSource(event.target.value)}
                            required
                            maxLength={128}
                            disabled={locked}
                        />
                    </SimpleField>
                </div>
                <div className="valuation-actions">
                    <button className="primary-button" type="submit" disabled={!canSave}>
                        {t("保存价格", "Save price")}
                    </button>
                    {(editing || state.status === "conflict") && (
                        <button
                            className="secondary-button"
                            type="button"
                            disabled={locked}
                            onClick={() => {
                                controller.dismiss();
                                edit(null);
                            }}
                        >
                            {t("添加另一条价格", "Add another price")}
                        </button>
                    )}
                </div>
            </form>
            {state.status === "submitting" && (
                <p role="status">{t("正在保存价格…", "Saving price…")}</p>
            )}
            {state.status === "confirmed" && (
                <p role="status">{t("价格已保存。", "Price saved.")}</p>
            )}
            {state.status === "unknown" && (
                <p role="alert">
                    {t("保存结果尚未确认。", "The save result is unconfirmed.")}{" "}
                    <button
                        type="button"
                        className="secondary-button"
                        onClick={() => {
                            void controller.retry(session).catch(setLocalError);
                        }}
                    >
                        {t("重试原保存", "Retry original save")}
                    </button>
                </p>
            )}
            {state.status === "conflict" && (
                <p role="alert">
                    {t(
                        "该价格已变更。请先核对并载入当前版本。",
                        "This price has changed. Review and load the current version first.",
                    )}{" "}
                    {state.record && (
                        <button
                            type="button"
                            className="secondary-button"
                            onClick={() => {
                                edit(state.record);
                                controller.dismiss();
                            }}
                        >
                            {t("载入当前价格", "Load current price")}
                        </button>
                    )}
                </p>
            )}
            {Boolean(localError || state.status === "rejected") && (
                <p role="alert">
                    {localError instanceof AmountError ||
                    (localError instanceof ApiError && localError.code === "invalid_price")
                        ? t(
                              "请输入最多 18 位小数的正数价格。",
                              "Enter a positive decimal price with up to 18 fractional digits.",
                          )
                        : businessError(localError ?? state.error, locale)}
                </p>
            )}
        </section>
    );
}

export function ValuationWorkspace({
    locale,
    session,
    entities,
    assets,
    initialLedger,
    prices,
    onUnauthorized,
}: {
    locale: Locale;
    session: Session;
    entities: Entity[];
    assets: Asset[];
    initialLedger?: string;
    prices: PendingPrice;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const catalog = new Map(assets.map((asset) => [asset.asset_id, asset.code]));
    const assetName = (id: string) =>
        catalog.has(id) && catalog.get(id) !== id ? `${catalog.get(id)} · ${id}` : id;
    const state = useSyncExternalStore(prices.subscribe, prices.getSnapshot);
    const [savedPrice, setSavedPrice] = useState<ManualPrice | null>(null);
    useEffect(() => {
        if (state.status === "confirmed" && state.record) setSavedPrice(state.record);
    }, [state.status, state.record]);
    return (
        <>
            <ReportPanel
                t={t}
                locale={locale}
                assetName={assetName}
                entities={entities}
                initialLedger={initialLedger}
                savedPrice={savedPrice}
                onUnauthorized={onUnauthorized}
            />
            <PricePanel
                t={t}
                locale={locale}
                assetName={assetName}
                assets={assets}
                session={session}
                controller={prices}
                onUnauthorized={onUnauthorized}
            />
        </>
    );
}
