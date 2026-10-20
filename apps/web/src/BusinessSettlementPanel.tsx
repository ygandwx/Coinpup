import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { ApiError } from "./api";
import type { Session } from "./api";
import type { Account, Asset, Category } from "./ledger-api";
import type { Locale } from "./i18n";
import { listMasterData } from "./business-api";
import type { Party } from "./business-api";
import type {
    BusinessPaymentCreate,
    BusinessPaymentDetail,
    BusinessAllocationDetail,
    InitialAllocationCreate,
} from "./business-settlement-api";
import type { PendingBusinessSettlementController } from "./pending-business-settlement";
import { localDraftDate } from "./draft-fields";
import { formatAmount } from "./money";
import { paymentFieldError, allocationFieldError } from "./settlement-fields";
import type { AllocationSource } from "./settlement-fields";
import { BusinessAllocationFields } from "./BusinessAllocationFields";
import { businessError } from "./business-errors";
import { BusinessPaymentFields } from "./BusinessPaymentFields";
import { BusinessSettlementHistory } from "./BusinessSettlementHistory";
import { BusinessSettlementResult } from "./BusinessSettlementResult";
import type { SettlementReviewData } from "./BusinessSettlementResult";
import { BusinessAllocationReturnForm } from "./BusinessAllocationReturnForm";
import type { ReturnEditor } from "./BusinessAllocationReturnForm";
import { BusinessAllocationReturnHistory } from "./BusinessAllocationReturnHistory";
import type { AllocationReturnCreate } from "./allocation-return-api";
import type { AllocationReturnReview } from "./allocation-return-intent";
import { BusinessRefundForm } from "./BusinessRefundForm";
import type { RefundEditor } from "./BusinessRefundForm";
import type { RefundCreate } from "./refund-api";
import type { RefundReview } from "./refund-intent";
import { getBusinessPayment } from "./business-settlement-api";
import { BusinessRefundHistory } from "./BusinessRefundHistory";
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
    const [returnEditor, setReturnEditor] = useState<ReturnEditor | null>(null);
    const [returnsOpen, setReturnsOpen] = useState(false);
    const [refundEditor, setRefundEditor] = useState<RefundEditor | null>(null);
    const [refundsOpen, setRefundsOpen] = useState(false);
    const [existing, setExisting] = useState<{
        payment: BusinessPaymentDetail;
        name: string | null;
    } | null>(null);
    const [allocation, setAllocation] = useState<InitialAllocationCreate | null>(null);
    const [sources, setSources] = useState<readonly AllocationSource[]>([]);
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
    const open = !!fields || !!plan || !!existing || !!returnEditor || !!refundEditor;
    const needReferences = !!fields && !plan;
    const refs = {
        accounts,
        assets,
        categories,
        parties: parties?.ledger === ledgerId ? parties.rows : [],
    };
    const ready = parties?.ledger === ledgerId && !dataLoading && !archived;
    useEffect(() => {
        callbacks.current.onEditing(open || historyEditing || returnsOpen || refundsOpen);
        return () => callbacks.current.onEditing(false);
    }, [open, historyEditing, returnsOpen, refundsOpen]);
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
        setSources([]);
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
    function beginAllocation(payment: BusinessPaymentDetail, name: string | null) {
        if (archived || dataLoading || payment.ledger_id !== ledgerId || !controller.dismiss())
            return;
        setFields(null);
        setExisting({ payment, name });
        setSources([]);
        setAllocation({ id: crypto.randomUUID(), allocation_date: localDraftDate(), lines: [] });
        setReviewed(false);
        setInvalid(false);
        setError(null);
    }
    function beginReturn(allocation: BusinessAllocationDetail) {
        if (archived || dataLoading || allocation.ledger_id !== ledgerId || !controller.dismiss())
            return;
        setReturnEditor({
            id: crypto.randomUUID(),
            allocationId: allocation.id,
            paymentId: allocation.payment_id,
            date:
                localDraftDate() < allocation.allocation_date
                    ? allocation.allocation_date
                    : localDraftDate(),
            reason: "",
            amounts: {},
            capacity: null,
        });
        setFields(null);
        setExisting(null);
        setAllocation(null);
        setError(null);
    }
    async function submitAllocation(event: FormEvent) {
        event.preventDefault();
        if (
            !existing ||
            !allocation ||
            archived ||
            dataLoading ||
            !reviewed ||
            controller.getSnapshot().plan
        )
            return;
        if (allocationFieldError(allocation, existing.payment, sources, assets)) {
            setInvalid(true);
            return;
        }
        setInvalid(false);
        setError(null);
        const review: SettlementReviewData = {
            payment: {
                ledger_id: ledgerId,
                party_id: existing.payment.party_id,
                direction: existing.payment.direction,
                asset_id: existing.payment.asset_id,
                transaction_date: existing.payment.transaction_date,
                unapplied_amount: existing.payment.unapplied_amount,
            },
            partyName: existing.name ?? existing.payment.party_id,
            accountName:
                accounts.find((row) => row.id === existing.payment.account_id)?.name ??
                existing.payment.account_id,
            feeLabels: [],
            sources,
        };
        try {
            await controller.start(
                session,
                ledgerId,
                {
                    kind: "allocation",
                    body: { ...allocation, payment_id: existing.payment.id },
                },
                review,
            );
        } catch (caught) {
            fail(caught);
        }
    }
    function beginRefund(payment: BusinessPaymentDetail) {
        if (archived || dataLoading || !controller.dismiss()) return;
        setError(null);
        setRefundEditor({
            payment,
            capacity: null,
            selectedReturns: [],
            body: {
                id: crypto.randomUUID(),
                payment_id: payment.id,
                expected_version: 1,
                account_id: payment.account_id,
                asset_id: payment.asset_id,
                amount: "",
                payment_amount: "",
                transaction_date: localDraftDate(),
                reason: "",
                allocation_return_ids: [],
                fees: [],
            },
        });
    }
    async function editRefund() {
        if (!plan || plan.kind !== "refund") return;
        try {
            const body = JSON.parse(plan.bodyJson) as RefundCreate;
            const review = JSON.parse(plan.reviewedJson) as RefundReview;
            const payment = await getBusinessPayment(ledgerId, body.payment_id);
            if (controller.getSnapshot().plan !== plan) return;
            if (payment.id !== body.payment_id || payment.ledger_id !== ledgerId)
                throw new ApiError("server", 200, "invalid_response");
            if (!controller.dismiss()) return;
            setRefundEditor({
                payment,
                capacity: null,
                selectedReturns: review.returns,
                body: { ...body, id: crypto.randomUUID() },
            });
            setError(null);
        } catch (caught) {
            if (controller.getSnapshot().plan === plan) fail(caught);
        }
    }
    function close() {
        const saved = controller.getSnapshot().status === "confirmed";
        if (!controller.dismiss()) return;
        setFields(null);
        setExisting(null);
        setAllocation(null);
        setReturnEditor(null);
        setRefundEditor(null);
        setSources([]);
        setReviewed(false);
        setInvalid(false);
        setError(null);
        setHistoryRefresh((value) => value + 1);
        if (saved) callbacks.current.onSaved();
    }
    async function submit(event: FormEvent) {
        event.preventDefault();
        if (!fields || !ready || !reviewed || controller.getSnapshot().plan) return;
        const paymentContext = {
            ledger_id: ledgerId,
            party_id: fields.party_id,
            direction: fields.direction,
            asset_id: fields.asset_id,
            transaction_date: fields.transaction_date,
            unapplied_amount: fields.amount,
        };
        if (
            paymentFieldError(ledgerId, fields, refs) ||
            (fields.initial_allocation &&
                allocationFieldError(fields.initial_allocation, paymentContext, sources, assets))
        ) {
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
            sources,
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
                    <h2>
                        {refundEditor || plan?.kind === "refund"
                            ? t("实际退款", "Actual refund")
                            : returnEditor || plan?.kind === "return"
                              ? t("分摊退回", "Return allocation")
                              : existing || plan?.kind === "allocation"
                                ? t("追加分摊", "Apply existing payment")
                                : t("实际收付款", "Actual payment")}
                    </h2>
                    {plan ? (
                        <BusinessSettlementResult
                            pending={pending}
                            locale={locale}
                            onRetry={retry}
                            onClose={close}
                            onEdit={() => {
                                if (plan.kind === "refund") {
                                    void editRefund();
                                    return;
                                }
                                if (controller.dismiss()) {
                                    if (plan.kind === "return") {
                                        const body = JSON.parse(
                                            plan.bodyJson,
                                        ) as AllocationReturnCreate;
                                        const review = JSON.parse(
                                            plan.reviewedJson,
                                        ) as AllocationReturnReview;
                                        setReturnEditor({
                                            id: crypto.randomUUID(),
                                            allocationId: review.capacity.id,
                                            paymentId: body.payment_id,
                                            date: body.return_date,
                                            reason: body.reason,
                                            amounts: Object.fromEntries(
                                                body.lines.map((line) => [
                                                    line.source_allocation_line_id,
                                                    line.document_amount,
                                                ]),
                                            ),
                                            capacity: null,
                                        });
                                    }
                                    setReviewed(false);
                                    setError(null);
                                }
                            }}
                        />
                    ) : refundEditor ? (
                        <BusinessRefundForm
                            ledgerId={ledgerId}
                            locale={locale}
                            accounts={accounts}
                            assets={assets}
                            categories={categories}
                            draft={refundEditor}
                            disabled={archived || dataLoading}
                            onChange={(update) =>
                                setRefundEditor((value) => (value ? update(value) : value))
                            }
                            onSubmit={async (body, review) => {
                                if (archived || dataLoading || controller.getSnapshot().plan)
                                    return;
                                try {
                                    await controller.start(
                                        session,
                                        ledgerId,
                                        { kind: "refund", body },
                                        review,
                                    );
                                } catch (caught) {
                                    fail(caught);
                                }
                            }}
                            onCancel={close}
                            onUnauthorized={onUnauthorized}
                        />
                    ) : returnEditor ? (
                        <BusinessAllocationReturnForm
                            ledgerId={ledgerId}
                            locale={locale}
                            assets={assets}
                            draft={returnEditor}
                            disabled={archived || dataLoading}
                            onChange={(update) =>
                                setReturnEditor((value) => (value ? update(value) : value))
                            }
                            onSubmit={async (body, review) => {
                                if (archived || dataLoading || controller.getSnapshot().plan)
                                    return;
                                await controller.start(
                                    session,
                                    ledgerId,
                                    { kind: "return", body },
                                    review,
                                );
                            }}
                            onCancel={close}
                            onUnauthorized={onUnauthorized}
                        />
                    ) : existing && allocation ? (
                        <form
                            onSubmit={submitAllocation}
                            aria-label={t("已有付款分摊", "Existing payment allocation")}
                        >
                            <dl className="confirmation-facts">
                                <dt>{t("原收付款ID", "Original payment ID")}</dt>
                                <dd>{existing.payment.id}</dd>
                                <dt>
                                    {t("往来单位（当前名称）", "Business party (current name)")}
                                </dt>
                                <dd>{existing.name ?? existing.payment.party_id}</dd>
                                <dt>{t("交易日", "Transaction date")}</dt>
                                <dd>{existing.payment.transaction_date}</dd>
                                <dt>{t("原本金", "Original principal")}</dt>
                                <dd>
                                    {formatAmount(existing.payment.amount, locale)}{" "}
                                    {existing.payment.asset_id}
                                </dd>
                                <dt>{t("原说明", "Original description")}</dt>
                                <dd>{existing.payment.description || "—"}</dd>
                            </dl>
                            <p>
                                {t("未分配原币数量", "Unapplied original quantity")}:{" "}
                                {formatAmount(existing.payment.unapplied_amount, locale)}{" "}
                                {existing.payment.asset_id}
                            </p>
                            <p className="help-text">
                                {t(
                                    "仅应用已有收付款，不会再次扣款或重新确认经营收支。",
                                    "Apply an existing payment without another cash movement or recognizing income or expense again.",
                                )}
                            </p>
                            <BusinessAllocationFields
                                value={allocation}
                                payment={existing.payment}
                                sources={sources}
                                assets={assets}
                                locale={locale}
                                locked={archived || dataLoading}
                                onUnauthorized={onUnauthorized}
                                onChange={(group, selected) => {
                                    setAllocation(group);
                                    setSources(selected);
                                    setReviewed(false);
                                }}
                            />
                            <label className="archive-toggle">
                                <input
                                    type="checkbox"
                                    checked={reviewed}
                                    disabled={archived || dataLoading}
                                    onChange={(event) => setReviewed(event.target.checked)}
                                />
                                {t(
                                    "已核对原收付款、所选源行、两侧数量和分摊日期。",
                                    "I reviewed the original payment, source lines, both quantities and allocation date.",
                                )}
                            </label>
                            {invalid && (
                                <p role="alert">
                                    {t(
                                        "请检查源行、精确数量、未结余额和日期下界；期间须开放。",
                                        "Check source lines, exact quantities, available balances and date lower bounds; the period must be open.",
                                    )}
                                </p>
                            )}
                            <div className="form-actions">
                                <button
                                    type="submit"
                                    className="primary-button"
                                    disabled={archived || dataLoading || !reviewed}
                                >
                                    {t("确认分摊", "Apply payment")}
                                </button>
                                <button type="button" onClick={close}>
                                    {t("取消", "Cancel")}
                                </button>
                            </div>
                        </form>
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
                                    identityLocked={sources.length > 0}
                                    onChange={(value) => {
                                        setFields(value);
                                        setReviewed(false);
                                    }}
                                />
                                <label className="archive-toggle">
                                    <input
                                        type="checkbox"
                                        checked={!!fields.initial_allocation}
                                        disabled={!ready || !fields.party_id || !fields.asset_id}
                                        onChange={(event) => {
                                            setFields({
                                                ...fields,
                                                initial_allocation: event.target.checked
                                                    ? {
                                                          id: crypto.randomUUID(),
                                                          allocation_date: fields.transaction_date,
                                                          lines: [],
                                                      }
                                                    : null,
                                            });
                                            setSources([]);
                                            setReviewed(false);
                                        }}
                                    />
                                    {t(
                                        "同时分摊到已确认单据",
                                        "Allocate to confirmed documents atomically",
                                    )}
                                </label>
                                {fields.initial_allocation && (
                                    <BusinessAllocationFields
                                        key={JSON.stringify([
                                            ledgerId,
                                            fields.party_id,
                                            fields.direction,
                                            fields.asset_id,
                                        ])}
                                        value={fields.initial_allocation}
                                        sources={sources}
                                        assets={assets}
                                        payment={{
                                            ledger_id: ledgerId,
                                            party_id: fields.party_id,
                                            direction: fields.direction,
                                            asset_id: fields.asset_id,
                                            transaction_date: fields.transaction_date,
                                            unapplied_amount: fields.amount,
                                        }}
                                        locale={locale}
                                        locked={!ready}
                                        onUnauthorized={onUnauthorized}
                                        onChange={(group, selected) => {
                                            setFields({ ...fields, initial_allocation: group });
                                            setSources(selected);
                                            setReviewed(false);
                                        }}
                                    />
                                )}
                                <p className="help-text">
                                    {t(
                                        "未分摊部分保留为预收或预付；勾选分摊时整笔一起保存，任何错误都不会留下部分付款。",
                                        "The unapplied portion remains an advance or prepayment. Selected allocations and payment are saved together; any error rolls back the whole payment.",
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
                                        "已核对收付方向、原币本金、账户、独立手续费及所选分摊。",
                                        "I reviewed direction, original principal, account, separate fees and selected allocations.",
                                    )}
                                </label>
                                {invalid && (
                                    <p role="alert">
                                        {t(
                                            "请核对日期、金额精度、有效往来和账户、手续费分类及所选单据未结余额。分摊日不得早于付款日和开票日。",
                                            "Check dates, precision, active references, fee categories and selected outstanding amounts. Allocation cannot precede payment or issue dates.",
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
            ) : refundsOpen ? (
                <BusinessRefundHistory
                    ledgerId={ledgerId}
                    locale={locale}
                    onClose={() => setRefundsOpen(false)}
                    onUnauthorized={onUnauthorized}
                />
            ) : returnsOpen ? (
                <BusinessAllocationReturnHistory
                    ledgerId={ledgerId}
                    locale={locale}
                    onClose={() => setReturnsOpen(false)}
                    onUnauthorized={onUnauthorized}
                />
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
                    {!historyEditing && (
                        <button type="button" onClick={() => setReturnsOpen(true)}>
                            {t("分摊退回历史", "Allocation return history")}
                        </button>
                    )}
                    {!historyEditing && (
                        <button type="button" onClick={() => setRefundsOpen(true)}>
                            {t("实际退款历史", "Actual refund history")}
                        </button>
                    )}
                    <BusinessSettlementHistory
                        key={historyRefresh}
                        ledgerId={ledgerId}
                        locale={locale}
                        accounts={accounts}
                        onAllocate={archived || dataLoading ? undefined : beginAllocation}
                        onReturn={archived || dataLoading ? undefined : beginReturn}
                        onRefund={archived || dataLoading ? undefined : beginRefund}
                        onEditing={setHistoryEditing}
                        onUnauthorized={onUnauthorized}
                    />
                </>
            )}
        </section>
    );
}
