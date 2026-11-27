import { AccountPicker } from "./AccountPicker";
import { useId, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import { businessError } from "./business-errors";
import type { Locale } from "./i18n";
import {
    getInterledgerAdvance,
    getInterledgerAdvances,
    getInterledgerSettings,
    setInterledgerSettings,
} from "./interledger-api";
import type { InterledgerAdvance, InterledgerDetail, InterledgerSettings } from "./interledger-api";
import { listAccounts, listCategories } from "./ledger-api";
import type { Account, Asset, Category, Entity } from "./ledger-api";
import { AmountError, formatAmount, parseAmount } from "./money";
import type { PendingInterledger } from "./pending-interledger";

type Translate = (zh: string, en: string) => string;
const size = 25;
const localError = (code: string) => new ApiError("server", 422, code);
function message(error: unknown, locale: Locale, t: Translate) {
    if (error instanceof AmountError)
        return t(
            "请按该资产的精度输入十进制金额。",
            "Enter a decimal amount within the asset precision.",
        );
    if (error instanceof ApiError) {
        switch (error.code) {
            case "interledger_disabled":
                return t("先开启代付和偿还功能。", "Enable advances and repayments first.");
            case "over_repayment":
                return t("偿还金额超过未结本金。", "The repayment exceeds the outstanding amount.");
            case "repayment_before_advance":
                return t("偿还日期不可早于代付日期。", "The repayment date precedes the advance.");
            case "amount_positive":
                return t("请输入正金额。", "Enter a positive amount.");
            case "idempotency_conflict":
                return t(
                    "此请求键对应另一条请求，请核对原始意图。",
                    "This key belongs to a different request. Review the original intent.",
                );
        }
    }
    return businessError(error, locale);
}

type Option = { id: string; name: string };
function Choice({
    name,
    label,
    choices,
    placeholder,
    value,
    onChange,
}: {
    name: string;
    label: string;
    choices: Option[];
    placeholder: string;
    value?: string;
    onChange?: (value: string) => void;
}) {
    return (
        <label className="field">
            {label}
            <select
                aria-label={label}
                name={name}
                required
                value={value}
                onChange={onChange && ((event) => onChange(event.currentTarget.value))}
                defaultValue={value === undefined ? "" : undefined}
            >
                <option value="">{placeholder}</option>
                {choices.map(({ id, name: title }) => (
                    <option key={id} value={id}>
                        {title}
                    </option>
                ))}
            </select>
        </label>
    );
}

function AccountChoice({
    name,
    label,
    choices,
    placeholder,
    locale,
}: {
    name: string;
    label: string;
    choices: readonly Account[];
    placeholder: string;
    locale: Locale;
}) {
    const id = useId();
    return (
        <div className="field">
            <label htmlFor={id}>{label}</label>
            <AccountPicker
                id={id}
                name={name}
                locale={locale}
                placeholder={placeholder}
                accounts={choices}
            />
        </div>
    );
}

async function all<T>(read: (offset: number) => Promise<T[]>): Promise<T[]> {
    const items: T[] = [];
    for (let offset = 0; offset <= 100000; offset += 100) {
        const page = await read(offset);
        items.push(...page);
        if (page.length < 100) return items;
    }
    throw new ApiError("server", null, "invalid_pagination");
}

function EntryForm({
    t,
    locale,
    active,
    assets,
    advance,
    session,
    controller,
    locked,
    onError,
    onResult,
}: {
    t: Translate;
    locale: Locale;
    active: Entity[];
    assets: Asset[];
    advance: InterledgerAdvance | null;
    session: Session;
    controller: PendingInterledger;
    locked: boolean;
    onError: (error: unknown) => void;
    onResult: () => void;
}) {
    const [payer, setPayer] = useState(advance?.payer_ledger_id ?? "");
    const [beneficiary, setBeneficiary] = useState(advance?.beneficiary_ledger_id ?? "");
    const [assetId, setAssetId] = useState(advance?.asset_id ?? "");
    const [references, setReferences] = useState<{
        pair: string;
        payerAccounts: Account[];
        beneficiaryAccounts: Account[];
        categories: Category[];
    } | null>(null);
    const key = JSON.stringify([payer, beneficiary]);
    const selected = references?.pair === key ? references : null;
    const problem = useRef(onError);
    problem.current = onError;
    useEffect(() => {
        if (!payer || !beneficiary || payer === beneficiary) return;
        const abort = new AbortController();
        const options = (offset: number) => ({ offset, limit: 100, include_archived: false });
        void Promise.all([
            all((offset) => listAccounts(payer, options(offset), abort.signal)),
            advance
                ? all((offset) => listAccounts(beneficiary, options(offset), abort.signal))
                : Promise.resolve([]),
            advance
                ? Promise.resolve([])
                : all((offset) => listCategories(beneficiary, options(offset), abort.signal)),
        ])
            .then(([payerAccounts, beneficiaryAccounts, categories]) => {
                if (!abort.signal.aborted)
                    setReferences({ pair: key, payerAccounts, beneficiaryAccounts, categories });
            })
            .catch((error: unknown) => {
                if (!abort.signal.aborted) problem.current(error);
            });
        return () => abort.abort();
    }, [payer, beneficiary, key, advance]);

    const accounts = (values: Account[] | undefined) =>
        (values ?? []).filter(
            (account) => !account.archived && account.asset_ids.includes(assetId),
        );
    const available = assets.filter(
        ({ enabled, asset_id }) =>
            enabled &&
            (selected?.payerAccounts ?? []).some((account) => account.asset_ids.includes(asset_id)),
    );
    const asset = assets.find(({ asset_id }) => asset_id === assetId);
    const ledgers = active.map((entity) => ({ id: entity.ledger.id, name: entity.name }));
    const payers = ledgers.filter(({ id }) => id !== beneficiary);
    const beneficiaries = ledgers.filter(({ id }) => id !== payer);
    const categories = (selected?.categories ?? []).filter(
        (category) => category.kind === "expense" && !category.archived,
    );
    const placeholder = t("请选择", "Select");
    const title = advance ? t("登记偿还", "Record repayment") : t("登记代付", "Register advance");
    async function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (locked || !asset || !selected) return;
        const values = new FormData(event.currentTarget);
        const value = (name: string) => String(values.get(name) ?? "");
        const amount = value("amount");
        const transaction_date = value("transaction_date");
        const description = value("description").trim();
        try {
            if (
                advance &&
                parseAmount(amount, asset.scale) >
                    parseAmount(advance.outstanding_amount, asset.scale)
            )
                throw localError("over_repayment");
            if (advance && transaction_date < advance.transaction_date)
                throw localError("repayment_before_advance");
            await controller.start(
                session,
                payer,
                asset.scale,
                advance
                    ? {
                          kind: "repayment",
                          advanceId: advance.id,
                          body: {
                              beneficiary_payment_account_id: value("payment_account"),
                              payer_receipt_account_id: value("payer_account"),
                              transaction_date,
                              amount,
                              description,
                          },
                      }
                    : {
                          kind: "advance",
                          body: {
                              payer_ledger_id: payer,
                              beneficiary_ledger_id: beneficiary,
                              payer_account_id: value("payer_account"),
                              expense_category_id: value("category"),
                              asset_id: assetId,
                              transaction_date,
                              amount,
                              description,
                          },
                      },
            );
            onResult();
        } catch (error) {
            onError(error);
        }
    }
    return (
        <form aria-label={title} onSubmit={(event) => void submit(event)}>
            <fieldset disabled={locked} className="form-grid">
                {!advance && (
                    <>
                        <Choice
                            name="payer"
                            label={t("原付款方账本", "Original payer ledger")}
                            choices={payers}
                            placeholder={placeholder}
                            value={payer}
                            onChange={(value) => {
                                setPayer(value);
                                setAssetId("");
                            }}
                        />
                        <Choice
                            name="beneficiary"
                            label={t("受益方账本", "Beneficiary ledger")}
                            choices={beneficiaries}
                            placeholder={placeholder}
                            value={beneficiary}
                            onChange={(value) => {
                                setBeneficiary(value);
                                setAssetId("");
                            }}
                        />
                        <Choice
                            name="asset"
                            label={t("原币资产", "Original asset")}
                            choices={available.map(({ asset_id }) => ({
                                id: asset_id,
                                name: asset_id,
                            }))}
                            placeholder={placeholder}
                            value={assetId}
                            onChange={setAssetId}
                        />
                    </>
                )}
                {advance && (
                    <AccountChoice
                        locale={locale}
                        name="payment_account"
                        label={t("受益方付款账户", "Beneficiary payment account")}
                        choices={accounts(selected?.beneficiaryAccounts)}
                        placeholder={placeholder}
                    />
                )}
                <AccountChoice
                    locale={locale}
                    key={`${key}:${assetId}:account`}
                    name="payer_account"
                    label={
                        advance
                            ? t("原付款方收款账户", "Original payer receipt account")
                            : t("原付款方付款账户", "Original payer money account")
                    }
                    choices={accounts(selected?.payerAccounts)}
                    placeholder={placeholder}
                />
                {!advance && (
                    <Choice
                        key={key}
                        name="category"
                        label={t("受益方费用分类", "Beneficiary expense category")}
                        choices={categories}
                        placeholder={placeholder}
                    />
                )}
                <label className="field">
                    {t("金额", "Amount")}
                    <input name="amount" inputMode="decimal" required maxLength={40} />
                </label>
                <label className="field">
                    {t("交易日", "Transaction date")}
                    <input
                        name="transaction_date"
                        type="date"
                        required
                        min={advance?.transaction_date}
                        defaultValue={new Date().toISOString().slice(0, 10)}
                    />
                </label>
                <label className="field">
                    {t("说明", "Description")}
                    <input name="description" maxLength={2000} />
                </label>
            </fieldset>
            <button
                className="primary-button"
                type="submit"
                disabled={locked || !selected || !asset}
            >
                {title}
            </button>
        </form>
    );
}

export function InterledgerPanel({
    locale,
    session,
    entities,
    assets,
    controller,
    refresh,
    onChanged,
    onUnauthorized,
}: {
    locale: Locale;
    session: Session;
    entities: Entity[];
    assets: Asset[];
    controller: PendingInterledger;
    refresh: number;
    onChanged: () => void;
    onUnauthorized: () => void;
}) {
    const t: Translate = (zh, en) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const locked = !["idle", "confirmed", "rejected"].includes(pending.status);
    const [page, setPage] = useState(0);
    const [reload, setReload] = useState(0);
    const [listing, setListing] = useState<{
        key: string;
        settings: InterledgerSettings;
        rows: InterledgerAdvance[];
    } | null>(null);
    const [detailId, setDetailId] = useState<string | null>(null);
    const [detail, setDetail] = useState<{ key: string; row: InterledgerDetail } | null>(null);
    const [form, setForm] = useState<"advance" | "repayment" | null>(null);
    const [failure, setFailure] = useState<unknown>(null);
    const [toggling, setToggling] = useState(false);
    const inFlight = useRef<AbortController | null>(null);
    const unauthorized = useRef(onUnauthorized);
    unauthorized.current = onUnauthorized;
    const active = entities.filter((entity) => !entity.archived);
    const key = JSON.stringify([session.user.id, refresh, reload, page]);
    const current = listing?.key === key ? listing : null;
    const detailKey = JSON.stringify([session.user.id, refresh, reload, detailId]);
    const selected = detail?.key === detailKey ? detail.row : null;
    const name = (ledgerId: string) =>
        entities.find((entity) => entity.ledger.id === ledgerId)?.name ?? ledgerId;
    const money = (raw: string, assetId: string) => `${formatAmount(raw, locale)} ${assetId}`;
    function onError(error: unknown) {
        if (error instanceof ApiError && error.kind === "unauthorized") unauthorized.current();
        else setFailure(error);
    }
    const errorHandler = useRef(onError);
    errorHandler.current = onError;
    useEffect(() => () => inFlight.current?.abort(), []);
    useEffect(() => {
        const abort = new AbortController();
        void Promise.all([
            getInterledgerSettings(abort.signal),
            getInterledgerAdvances(page * size, size, abort.signal),
        ])
            .then(([settings, rows]) => {
                if (!abort.signal.aborted) setListing({ key, settings, rows });
            })
            .catch((error: unknown) => {
                if (!abort.signal.aborted) errorHandler.current(error);
            });
        return () => abort.abort();
    }, [key, page]);
    useEffect(() => {
        if (!detailId) return;
        const abort = new AbortController();
        void getInterledgerAdvance(detailId, abort.signal)
            .then((row) => {
                if (!abort.signal.aborted) setDetail({ key: detailKey, row });
            })
            .catch((error: unknown) => {
                if (!abort.signal.aborted) errorHandler.current(error);
            });
        return () => abort.abort();
    }, [detailId, detailKey]);

    function onResult() {
        const result = controller.getSnapshot();
        if (result.status === "auth-required") unauthorized.current();
        if (result.status === "confirmed" && result.record) {
            const receipt = result.record;
            setForm(null);
            setFailure(null);
            setDetailId("advance_id" in receipt ? receipt.advance_id : receipt.id);
            setPage(0);
            onChanged();
        }
    }
    async function toggle() {
        if (!current || inFlight.current || locked) return;
        const abort = new AbortController();
        inFlight.current = abort;
        setToggling(true);
        setFailure(null);
        try {
            await setInterledgerSettings(
                session.csrf_token,
                !current.settings.enabled,
                abort.signal,
            );
            if (!abort.signal.aborted) setForm(null);
        } catch (error) {
            if (!abort.signal.aborted) onError(error);
        } finally {
            inFlight.current = null;
            if (!abort.signal.aborted) {
                setToggling(false);
                setReload((value) => value + 1);
            }
        }
    }
    const editable = current?.settings.enabled && !locked && !toggling;

    return (
        <section
            className="business-panel interledger-panel"
            aria-label={t("代付与偿还", "Advances and repayments")}
        >
            {current && (
                <>
                    <p>
                        {current.settings.enabled
                            ? t("代付已开启。", "Advances are enabled.")
                            : t("代付已关闭。", "Advances are disabled.")}
                    </p>
                    <button
                        type="button"
                        disabled={locked || toggling}
                        onClick={() => void toggle()}
                    >
                        {current.settings.enabled
                            ? t("关闭代付", "Disable advances")
                            : t("开启代付", "Enable advances")}
                    </button>
                    {editable && active.length > 1 && form !== "advance" && (
                        <button
                            className="secondary-button"
                            type="button"
                            onClick={() => setForm("advance")}
                        >
                            {t("新增代付", "New advance")}
                        </button>
                    )}
                </>
            )}
            {failure != null && <p role="alert">{message(failure, locale, t)}</p>}
            {!current && failure == null && (
                <p role="status">{t("正在读取代付历史…", "Loading advance history…")}</p>
            )}
            {pending.status === "submitting" && (
                <p role="status">{t("正在提交…", "Submitting…")}</p>
            )}
            {pending.status === "unknown" && (
                <div role="alert">
                    <p>
                        {t(
                            "结果待确认。使用原请求和原键重试。",
                            "The result is unconfirmed. Retry the original request and key.",
                        )}
                    </p>
                    <button
                        type="button"
                        onClick={() => void controller.retry(session).then(onResult, onError)}
                    >
                        {t("重试原请求", "Retry original request")}
                    </button>
                </div>
            )}
            {pending.status === "rejected" && (
                <p role="alert">{message(pending.error, locale, t)}</p>
            )}
            {pending.status === "confirmed" && (
                <p role="status">
                    {t("两边凭证回执已核对。", "Both ledger postings are confirmed.")}
                </p>
            )}
            {current?.settings.enabled &&
                (form === "advance" || (form === "repayment" && selected)) && (
                    <EntryForm
                        locale={locale}
                        key={form === "advance" ? form : selected?.id}
                        t={t}
                        active={active}
                        assets={assets}
                        advance={form === "advance" ? null : selected}
                        session={session}
                        controller={controller}
                        locked={locked || toggling}
                        onError={onError}
                        onResult={onResult}
                    />
                )}
            <h2>{t("代付历史", "Advance history")}</h2>
            {current?.rows.length === 0 && (
                <p>{t("本页没有代付记录。", "No advances on this page.")}</p>
            )}
            <ul>
                {current?.rows.map((row) => (
                    <li key={row.id}>
                        <button
                            type="button"
                            disabled={locked}
                            onClick={() => {
                                setDetailId(row.id);
                                setForm(null);
                            }}
                        >
                            {name(row.payer_ledger_id)} → {name(row.beneficiary_ledger_id)} ·{" "}
                            {money(row.amount, row.asset_id)} · {row.transaction_date}
                        </button>
                    </li>
                ))}
            </ul>
            {(page > 0 || current?.rows.length === size) && (
                <div className="actions">
                    <button
                        type="button"
                        disabled={!page || locked}
                        onClick={() => setPage(page - 1)}
                    >
                        {t("上一页", "Previous page")}
                    </button>
                    <button
                        type="button"
                        disabled={current?.rows.length !== size || locked}
                        onClick={() => setPage(page + 1)}
                    >
                        {t("下一页", "Next page")}
                    </button>
                </div>
            )}
            {selected && (
                <section aria-label={t("代付详情", "Advance details")}>
                    <h3>
                        {name(selected.payer_ledger_id)} → {name(selected.beneficiary_ledger_id)}
                    </h3>
                    <p>{selected.description}</p>
                    <p>
                        {t("未结本金", "Outstanding amount")}{" "}
                        <strong data-testid="interledger-outstanding">
                            {money(selected.outstanding_amount, selected.asset_id)}
                        </strong>
                    </p>
                    <p>
                        {t("代付关联", "Advance reference")} <code>{selected.id}</code>
                    </p>
                    <p>
                        {t("原付款方凭证", "Original payer posting")}{" "}
                        <code>{selected.payer_operation_id}</code>
                    </p>
                    <p>
                        {t("受益方凭证", "Beneficiary posting")}{" "}
                        <code>{selected.beneficiary_operation_id}</code>
                    </p>
                    {editable &&
                        active.some((entity) => entity.ledger.id === selected.payer_ledger_id) &&
                        active.some(
                            (entity) => entity.ledger.id === selected.beneficiary_ledger_id,
                        ) &&
                        /[1-9]/u.test(selected.outstanding_amount) && (
                            <button type="button" onClick={() => setForm("repayment")}>
                                {t("新增偿还", "New repayment")}
                            </button>
                        )}
                    <h3>{t("偿还历史", "Repayment history")}</h3>
                    <ul>
                        {selected.repayments.map((row) => (
                            <li key={row.id}>
                                {row.transaction_date} · {money(row.amount, selected.asset_id)} ·{" "}
                                {row.description}
                                <p>
                                    {t("原付款方凭证", "Original payer posting")}{" "}
                                    <code>{row.payer_operation_id}</code>
                                </p>
                                <p>
                                    {t("受益方凭证", "Beneficiary posting")}{" "}
                                    <code>{row.beneficiary_operation_id}</code>
                                </p>
                            </li>
                        ))}
                    </ul>
                </section>
            )}
        </section>
    );
}
