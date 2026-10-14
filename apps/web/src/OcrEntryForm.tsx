import { useEffect, useState } from "react";
import { PostingForm } from "./PostingForm";
import { OcrPaymentEvidence } from "./OcrPaymentEvidence";
import type { BusinessPaymentReceipt } from "./business-settlement-api";
import type { PostingInput } from "./pending-command";
import {
    formInput,
    initialEntry,
    preparedEntry,
    savedAction,
    savedDestination,
    replaceAction,
} from "./ocr-entry";
import type { Action } from "./ocr-entry";
import { OcrDuplicateNotice, OcrLinkPicker } from "./OcrRelations";
import type { Session } from "./api";
import type { Account, Asset, Category } from "./ledger-api";
import type { Locale } from "./i18n";
import { saveDraftReview } from "./ocr-api";
import type { ReviewView } from "./ocr-api";
import type { PendingConfirmationController } from "./pending-confirmations";

export function OcrEntryForm({
    session,
    locale,
    ledger,
    file,
    review,
    confirmed,
    accounts,
    assets,
    categories,
    locked,
    dirty,
    controller,
    onSaved,
    onError,
    onEditing,
}: {
    session: Session;
    locale: Locale;
    ledger: string;
    file: string;
    review: ReviewView;
    confirmed: string[];
    accounts: Account[];
    assets: Asset[];
    categories: Category[];
    locked: boolean;
    dirty: boolean;
    controller: PendingConfirmationController;
    onSaved: (review: ReviewView) => void;
    onError: (error: unknown) => void;
    onEditing: (value: boolean) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [kind, setKind] = useState<Action["kind"] | "">("");
    const [linkOpen, setLinkOpen] = useState(false);
    const [paymentOpen, setPaymentOpen] = useState(false);
    const [paymentProof, setPaymentProof] = useState<{
        key: string;
        receipt: BusinessPaymentReceipt;
    } | null>(null);
    const [duplicates, setDuplicates] = useState({ ready: false, acknowledged: false });
    const [input, setInput] = useState<PostingInput | null>(null),
        [busy, setBusy] = useState(false);
    const action = savedAction(review.review.entry);
    const target = savedDestination(review.review.entry) ?? { ledger, file };
    const entry = action && "command" in action ? action : null;
    const paymentMode = kind ? kind === "link_payment" : action?.kind === "link_payment";
    const paymentKey = `${session.user.id}/${ledger}/${review.draft_id}/${review.version}/${target.ledger}/${target.file}/${action?.kind === "link_payment" ? `${action.payment_id}/${action.expected_version}` : ""}`;
    const paymentReady = action?.kind !== "link_payment" || paymentProof?.key === paymentKey;
    const changedKind = !!kind && kind !== action?.kind;
    const complete = review.fields.every(
        (field) => !field.requires_confirmation || confirmed.includes(field.path),
    );
    useEffect(() => {
        onEditing(input !== null || linkOpen || paymentOpen || busy);
        return () => onEditing(false);
    }, [input, linkOpen, paymentOpen, busy, onEditing]);
    async function save(value: Action) {
        setBusy(true);
        try {
            const next = await saveDraftReview(session.csrf_token, ledger, review.draft_id, {
                expected_version: review.version,
                review: { confirmed, entry: replaceAction(review.review.entry, value) },
            });
            onSaved(next);
            setInput(null);
            setLinkOpen(false);
            setPaymentOpen(false);
            setKind(value.kind);
        } catch (error) {
            onError(error);
        } finally {
            setBusy(false);
        }
    }
    async function confirm() {
        if (
            !action ||
            dirty ||
            !complete ||
            locked ||
            busy ||
            !duplicates.ready ||
            changedKind ||
            !paymentReady
        )
            return;
        try {
            await controller.start(session, [
                {
                    ledgerId: ledger,
                    draftId: review.draft_id,
                    ...(action.kind === "link_payment" && paymentProof?.key === paymentKey
                        ? { originalPayment: paymentProof.receipt }
                        : {}),
                    body: {
                        expected_version: review.version,
                        target_ledger_id: target.ledger,
                        target_file_id: target.file,
                        confirmed,
                        duplicate_ack: duplicates.acknowledged,
                        entry: action,
                    },
                },
            ]);
        } catch (error) {
            onError(error);
        }
    }
    if (review.status !== "draft") return null;
    return (
        <section aria-label={t("人工确认入账", "Human confirmation")}>
            <h4>
                {paymentMode
                    ? t("付款证据关联", "Payment evidence link")
                    : t("入账草稿", "Prepared entry")}
            </h4>
            <p>
                {paymentMode
                    ? t(
                          "保存只保留关联草稿；最终确认只添加原件证据，不改资金和费用。",
                          "Saving prepares a link draft. Confirmation attaches evidence without changing funds or expenses.",
                      )
                    : t(
                          "选择业务类型、资金账户和分类，核对日期与金额后保存；最后明确确认才会入账。",
                          "Choose an entry type, account and categories, then check dates and amounts. Saving prepares a draft; only explicit confirmation posts it.",
                      )}
            </p>
            <OcrDuplicateNotice
                ledger={ledger}
                targetLedger={target.ledger}
                draft={review.draft_id}
                version={review.version}
                action={action}
                assets={assets}
                locale={locale}
                locked={locked || busy || input !== null || linkOpen || paymentOpen}
                onChange={setDuplicates}
                onError={onError}
            />
            {paymentOpen ? (
                <OcrPaymentEvidence
                    key={`${paymentKey}/picker`}
                    ledger={target.ledger}
                    locale={locale}
                    accounts={accounts}
                    locked={busy || locked}
                    onSave={(value) => void save(value)}
                    onReviewed={() => {}}
                    onError={onError}
                    onCancel={() => {
                        setPaymentOpen(false);
                        setKind(action?.kind ?? "");
                    }}
                />
            ) : linkOpen ? (
                <OcrLinkPicker
                    ledger={target.ledger}
                    locale={locale}
                    busy={busy || locked}
                    onSave={(value) => void save(value)}
                    onError={onError}
                    onCancel={() => {
                        setLinkOpen(false);
                        setKind(action?.kind === "link_payment" ? "" : (action?.kind ?? ""));
                    }}
                />
            ) : input ? (
                <PostingForm
                    locale={locale}
                    accounts={accounts}
                    assets={assets}
                    categories={categories}
                    initialInput={input}
                    showRetainedHelp={false}
                    busy={busy || locked}
                    submitLabel={t("保存入账草稿", "Save prepared entry")}
                    onSubmit={(value) => void save(preparedEntry(value))}
                    onCancel={() => {
                        setInput(null);
                        setKind(action?.kind === "link_payment" ? "" : (action?.kind ?? ""));
                    }}
                />
            ) : (
                <>
                    {action?.kind === "link_payment" && (
                        <>
                            <p>
                                {t("已保存付款关联", "Saved payment link")}: {action.payment_id} · v
                                {action.expected_version}
                            </p>
                            <OcrPaymentEvidence
                                key={paymentKey}
                                ledger={target.ledger}
                                locale={locale}
                                accounts={accounts}
                                action={action}
                                locked={busy || locked}
                                onError={onError}
                                onReviewed={(receipt) =>
                                    setPaymentProof(receipt ? { key: paymentKey, receipt } : null)
                                }
                            />
                        </>
                    )}
                    {action?.kind === "link" && (
                        <p>
                            {t("已保存关联", "Saved link")}: {action.operation_id} · v
                            {action.expected_version}
                        </p>
                    )}
                    {entry && (
                        <p>
                            {t("已保存", "Saved")}:{" "}
                            {
                                {
                                    opening: t("期初", "Opening"),
                                    income: t("收入", "Income"),
                                    expense: t("支出", "Expense"),
                                    transfer: t("转账", "Transfer"),
                                    exchange: t("换汇", "Exchange"),
                                }[entry.kind]
                            }{" "}
                            · {entry.command.transaction_date} ·{" "}
                            {"amount" in entry.command
                                ? entry.command.amount
                                : entry.command.source_amount}{" "}
                            {"asset_id" in entry.command
                                ? entry.command.asset_id
                                : entry.command.source_asset_id}
                        </p>
                    )}
                    <select
                        aria-label={t("选择入账类型", "Choose entry type")}
                        value={kind}
                        disabled={locked || busy}
                        onChange={(event) => setKind(event.target.value as Action["kind"] | "")}
                    >
                        <option value="">{t("请选择", "Choose")}</option>
                        {(
                            [
                                ["opening", "期初", "Opening"],
                                ["income", "收入", "Income"],
                                ["expense", "支出", "Expense"],
                                ["transfer", "转账", "Transfer"],
                                ["exchange", "换汇", "Exchange"],
                                ["link", "关联已有流水", "Link existing entry"],
                                [
                                    "link_payment",
                                    "关联经营收付款",
                                    "Link existing business payment",
                                ],
                            ] as const
                        ).map(([value, zh, en]) => (
                            <option value={value} key={value}>
                                {t(zh, en)}
                            </option>
                        ))}
                    </select>
                    <div className="ocr-actions">
                        <button
                            disabled={locked || busy || !kind}
                            onClick={() =>
                                kind === "link_payment"
                                    ? setPaymentOpen(true)
                                    : kind === "link"
                                      ? setLinkOpen(true)
                                      : kind &&
                                        setInput(
                                            initialEntry(kind, review, assets, confirmed, false),
                                        )
                            }
                        >
                            {t("填写入账草稿", "Prepare entry")}
                        </button>
                        <button
                            disabled={
                                locked ||
                                busy ||
                                !kind ||
                                kind === "link" ||
                                kind === "link_payment" ||
                                !confirmed.length
                            }
                            onClick={() =>
                                kind &&
                                kind !== "link" &&
                                kind !== "link_payment" &&
                                setInput(initialEntry(kind, review, assets, confirmed, true))
                            }
                        >
                            {t("使用已核对候选填写", "Use reviewed candidates")}
                        </button>
                        {entry && (
                            <button
                                disabled={locked || busy}
                                onClick={() => setInput(formInput(entry))}
                            >
                                {t("编辑入账草稿", "Edit prepared entry")}
                            </button>
                        )}
                        <button
                            disabled={
                                locked ||
                                busy ||
                                dirty ||
                                !action ||
                                !complete ||
                                !duplicates.ready ||
                                changedKind ||
                                !paymentReady
                            }
                            onClick={() => void confirm()}
                        >
                            {action?.kind === "link_payment"
                                ? t("确认付款关联", "Confirm payment link")
                                : action?.kind === "link"
                                  ? t("确认关联", "Confirm link")
                                  : t("确认入账", "Confirm and post")}
                        </button>
                    </div>
                    {!complete && (
                        <p>
                            {t(
                                "仍有字段需逐项核对。",
                                "Some fields still require individual review.",
                            )}
                        </p>
                    )}
                </>
            )}
        </section>
    );
}
