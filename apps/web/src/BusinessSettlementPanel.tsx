import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Account, Asset, Category } from "./ledger-api";
import type { Locale } from "./i18n";
import { listMasterData } from "./business-api";
import type { Party } from "./business-api";
import type { BusinessPaymentCreate } from "./business-settlement-api";
import type { PendingBusinessSettlementController } from "./pending-business-settlement";
import { localDraftDate } from "./draft-fields";
import { paymentFieldError } from "./settlement-fields";
import { businessError } from "./business-errors";
import { BusinessPaymentFields } from "./BusinessPaymentFields";
import { BusinessSettlementHistory } from "./BusinessSettlementHistory";
import { BusinessSettlementResult } from "./BusinessSettlementResult";
import type { SettlementReviewData } from "./BusinessSettlementResult";
import "./draft-form.css";

export function BusinessSettlementPanel({
    ledgerId,
    session,
    locale,
    controller,
    accounts,
    assets,
    categories,
    archived,
    dataLoading,
    onEditing,
    onUnauthorized,
    onSaved,
}: {
    ledgerId: string;
    session: Session;
    locale: Locale;
    controller: PendingBusinessSettlementController;
    accounts: Account[];
    assets: Asset[];
    categories: Category[];
    archived: boolean;
    dataLoading: boolean;
    onEditing: (value: boolean) => void;
    onUnauthorized: () => void;
    onSaved: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const pending = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const plan = pending.plan?.ledgerId === ledgerId ? pending.plan : null;
    const [fields, setFields] = useState<BusinessPaymentCreate | null>(null);
    const [parties, setParties] = useState<{
        ledger: string;
        rows: Party[];
    } | null>(null);
    const [referenceError, setReferenceError] = useState<unknown>(null);
    const [referenceRefresh, setReferenceRefresh] = useState(0);
    const [error, setError] = useState<unknown>(null);
    const [invalid, setInvalid] = useState(false),
        [reviewed, setReviewed] = useState(false);
    const [historyEditing, setHistoryEditing] = useState(false),
        [historyRefresh, setHistoryRefresh] = useState(0);
    const callbacks = useRef({ onEditing, onUnauthorized, onSaved });
    callbacks.current = { onEditing, onUnauthorized, onSaved };
    const open = !!fields || !!plan;
    const needReferences = !!fields && !plan;
    const refs = {
        accounts,
        assets,
        categories,
        parties: parties?.ledger === ledgerId ? parties.rows : [],
    };
    const ready = parties?.ledger === ledgerId && !dataLoading && !archived;
    useEffect(() => {
        callbacks.current.onEditing(open || historyEditing);
        return () => callbacks.current.onEditing(false);
    }, [open, historyEditing]);
    useEffect(() => {
        if (pending.status === "auth-required") callbacks.current.onUnauthorized();
    }, [pending.status]);
    useEffect(() => {
        if (!needReferences) return;
        const abort = new AbortController();
        async function read() {
            const rows: Party[] = [];
            for (let offset = 0; offset <= 100000; offset += 25) {
                const page = (await listMasterData(
                    ledgerId,
                    "parties",
                    offset,
                    abort.signal,
                )) as Party[];
                if (abort.signal.aborted) return;
                if (page.some((row) => row.ledger_id !== ledgerId))
                    throw new ApiError("server", 200, "invalid_response");
                rows.push(...page);
                if (page.length < 25) {
                    setParties({ ledger: ledgerId, rows });
                    setReferenceError(null);
                    return;
                }
            }
            throw new ApiError("server", null, "reference_limit");
        }
        read().catch((caught) => {
            if (abort.signal.aborted) return;
            if (caught instanceof ApiError && caught.status === 401)
                callbacks.current.onUnauthorized();
            else setReferenceError(caught);
        });
        return () => abort.abort();
    }, [ledgerId, needReferences, referenceRefresh]);
    function fail(caught: unknown) {
        if (caught instanceof ApiError && caught.status === 401) callbacks.current.onUnauthorized();
        else setError(caught);
    }
    function begin() {
        if (archived || dataLoading || !controller.dismiss()) return;
        setFields({
            id: crypto.randomUUID(),
            direction: "receipt",
            party_id: "",
            account_id: "",
            asset_id: "",
            amount: "",
            transaction_date: localDraftDate(),
            description: "",
            fees: [],
            initial_allocation: null,
        });
        setReviewed(false);
        setInvalid(false);
        setError(null);
    }
    function close() {
        const saved = controller.getSnapshot().status === "confirmed";
        if (!controller.dismiss()) return;
        setFields(null);
        setReviewed(false);
        setInvalid(false);
        setError(null);
        setHistoryRefresh((value) => value + 1);
        if (saved) callbacks.current.onSaved();
    }
    async function submit(event: FormEvent) {
        event.preventDefault();
        if (!fields || !ready || !reviewed || controller.getSnapshot().plan) return;
        if (paymentFieldError(ledgerId, fields, refs)) {
            setInvalid(true);
            return;
        }
        setInvalid(false);
        setError(null);
        const review: SettlementReviewData = {
            payment: {
                ledger_id: ledgerId,
                party_id: fields.party_id,
                direction: fields.direction,
                asset_id: fields.asset_id,
                transaction_date: fields.transaction_date,
                unapplied_amount: fields.amount,
            },
            partyName:
                refs.parties.find((row) => row.id === fields.party_id)?.name ?? fields.party_id,
            accountName:
                accounts.find((row) => row.id === fields.account_id)?.name ?? fields.account_id,
            feeLabels: (fields.fees ?? []).map((fee) => ({
                accountName:
                    accounts.find((row) => row.id === fee.account_id)?.name ?? fee.account_id,
                categoryName:
                    categories.find((row) => row.id === fee.category_id)?.name ?? fee.category_id,
            })),
            sources: [],
        };
        try {
            await controller.start(session, ledgerId, { kind: "payment", body: fields }, review);
        } catch (caught) {
            fail(caught);
        }
    }
    async function retry() {
        try {
            await controller.retry(session);
        } catch (caught) {
            fail(caught);
        }
    }
    return (
        <section
            className="business-panel business-settlement-workspace"
            aria-label={t("经营收付工作区", "Business settlement workspace")}
        >
            {open ? (
                <div className="business-settlement-panel">
                    <h2>{t("实际收付款", "Actual payment")}</h2>
                    {plan ? (
                        <BusinessSettlementResult
                            pending={pending}
                            locale={locale}
                            onRetry={retry}
                            onClose={close}
                            onEdit={() => {
                                if (controller.dismiss()) {
                                    setReviewed(false);
                                    setError(null);
                                }
                            }}
                        />
                    ) : (
                        fields && (
                            <form
                                onSubmit={submit}
                                aria-label={t("实际收付款表单", "Actual payment form")}
                            >
                                <BusinessPaymentFields
                                    value={fields}
                                    refs={refs}
                                    locale={locale}
                                    locked={dataLoading || archived}
                                    identityLocked={false}
                                    onChange={(value) => {
                                        setFields(value);
                                        setReviewed(false);
                                    }}
                                />
                                <p className="help-text">
                                    {t(
                                        "本次金额全部作为未分配的预收或预付保存。",
                                        "This payment will be saved entirely as an unapplied advance or prepayment.",
                                    )}
                                </p>
                                {!ready && !referenceError && (
                                    <p role="status">
                                        {archived
                                            ? t(
                                                  "主体已归档，不能新建收付款。",
                                                  "This entity is archived; new payments are unavailable.",
                                              )
                                            : t(
                                                  "正在读取有效资料…",
                                                  "Loading available references…",
                                              )}
                                    </p>
                                )}
                                {referenceError != null && (
                                    <>
                                        <p role="alert">{businessError(referenceError, locale)}</p>
                                        <button
                                            type="button"
                                            onClick={() =>
                                                setReferenceRefresh((value) => value + 1)
                                            }
                                        >
                                            {t("重读往来资料", "Reload party references")}
                                        </button>
                                    </>
                                )}
                                <label className="archive-toggle">
                                    <input
                                        type="checkbox"
                                        checked={reviewed}
                                        disabled={!ready}
                                        onChange={(event) => setReviewed(event.target.checked)}
                                    />
                                    {t(
                                        "已核对收付方向、原币本金、账户和独立手续费。",
                                        "I reviewed direction, original principal, account and separate fees.",
                                    )}
                                </label>
                                {invalid && (
                                    <p role="alert">
                                        {t(
                                            "请核对日期、正金额、资产精度、有效往来和账户，以及手续费分类。",
                                            "Check dates, positive quantities, asset precision, active party and accounts, and expense categories for fees.",
                                        )}
                                    </p>
                                )}
                                <div className="form-actions">
                                    <button
                                        type="submit"
                                        className="primary-button"
                                        disabled={!ready || !reviewed}
                                    >
                                        {t("登记实际收付款", "Record actual payment")}
                                    </button>
                                    <button type="button" onClick={close}>
                                        {t("取消", "Cancel")}
                                    </button>
                                </div>
                            </form>
                        )
                    )}
                    {error != null && <p role="alert">{businessError(error, locale)}</p>}
                </div>
            ) : (
                <>
                    {!historyEditing && (
                        <button
                            type="button"
                            className="primary-button"
                            disabled={archived || dataLoading}
                            onClick={begin}
                        >
                            {t("新建收付款", "New payment")}
                        </button>
                    )}
                    <BusinessSettlementHistory
                        key={historyRefresh}
                        ledgerId={ledgerId}
                        locale={locale}
                        accounts={accounts}
                        onEditing={setHistoryEditing}
                        onUnauthorized={onUnauthorized}
                    />
                </>
            )}
        </section>
    );
}
