import { useId, useState } from "react";
import type { CreditCreate } from "./credit-api";
import type { CreditReview } from "./credit-intent";
import { validCreditReview } from "./credit-intent";
import type { Category } from "./ledger-api";
import type { Locale } from "./i18n";
import { formatAmount } from "./money";
import "./draft-form.css";
import "./credit-form.css";

export function BusinessCreditForm({
    ledgerId,
    locale,
    body,
    review,
    categories,
    disabled,
    onChange,
    onSubmit,
    onCancel,
}: {
    ledgerId: string;
    locale: Locale;
    body: CreditCreate;
    review: CreditReview;
    categories: Category[];
    disabled: boolean;
    onChange: (update: (current: CreditCreate) => CreditCreate) => void;
    onSubmit: (body: CreditCreate, review: CreditReview) => void;
    onCancel: () => void;
}) {
    const prefix = useId();
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const reviewKey = JSON.stringify({ body, review });
    const [reviewedKey, setReviewedKey] = useState<string | null>(null),
        [invalid, setInvalid] = useState(false);
    const reviewed = reviewedKey === reviewKey;
    const amount = (value: string) => `${formatAmount(value, locale)} ${review.asset.asset_id}`;
    function change(update: (current: CreditCreate) => CreditCreate) {
        setReviewedKey(null);
        setInvalid(false);
        onChange(update);
    }
    function lineChange(
        id: string,
        field: "net_amount" | "tax_amount" | "recognition_date",
        value: string,
    ) {
        change((current) => ({
            ...current,
            lines: current.lines.map((line) =>
                line.source_line_id === id ? { ...line, [field]: value } : line,
            ),
        }));
    }
    return (
        <form
            className="credit-form"
            aria-label={t("登记贷项", "Record credit")}
            onSubmit={(event) => {
                event.preventDefault();
                if (disabled || !reviewed || !validCreditReview(ledgerId, body, review)) {
                    setInvalid(true);
                    return;
                }
                onSubmit(body, review);
            }}
        >
            <p className="help-text">
                {t(
                    "贷项按手填净额和税额冲减原分类，不自动退款；已结算部分须先明确退回分摊。",
                    "A credit reduces the original classification using manual net and tax amounts. It does not refund cash; explicitly return settled allocations first.",
                )}
            </p>
            <fieldset disabled={disabled}>
                <legend>{t("新的贷项事实", "New credit fact")}</legend>
                <div className="field">
                    <label htmlFor={`${prefix}-date`}>
                        {t("贷项交易日", "Credit transaction date")}
                    </label>
                    <input
                        id={`${prefix}-date`}
                        type="date"
                        value={body.credit_date}
                        min={review.capacity.issue_date}
                        onChange={(event) =>
                            change((current) => ({ ...current, credit_date: event.target.value }))
                        }
                    />
                </div>
                <div className="field">
                    <label htmlFor={`${prefix}-reason`}>{t("贷项原因", "Credit reason")}</label>
                    <textarea
                        id={`${prefix}-reason`}
                        value={body.reason}
                        maxLength={2000}
                        onChange={(event) =>
                            change((current) => ({ ...current, reason: event.target.value }))
                        }
                    />
                </div>
                {review.capacity.lines.map((source, index) => {
                    const selected = body.lines.find(
                        (row) => row.source_line_id === source.source_line_id,
                    );
                    const fieldId = `${prefix}-${source.source_line_id}`;
                    const category = categories.find((row) => row.id === source.category_id);
                    return (
                        <fieldset
                            key={source.source_line_id}
                            className="draft-line"
                            aria-label={`${t("原单据行", "Original document line")} ${index + 1}`}
                        >
                            <legend>{source.description}</legend>
                            <p>
                                {t("原分类", "Original category")}:{" "}
                                {category?.name ?? source.category_id}
                            </p>
                            <dl className="confirmation-facts">
                                <div>
                                    <dt>{t("原净额 / 税额", "Original net / tax")}</dt>
                                    <dd>
                                        {amount(source.net_amount)} / {amount(source.tax_amount)}
                                    </dd>
                                </div>
                                <div>
                                    <dt>{t("剩余净额 / 税额", "Remaining net / tax")}</dt>
                                    <dd>
                                        {amount(source.remaining_net_amount)} /{" "}
                                        {amount(source.remaining_tax_amount)}
                                    </dd>
                                </div>
                                <div>
                                    <dt>{t("净已分摊", "Net applied")}</dt>
                                    <dd>{amount(source.applied_amount)}</dd>
                                </div>
                                <div>
                                    <dt>{t("可贷含税额", "Available gross credit")}</dt>
                                    <dd>{amount(source.available_gross_amount)}</dd>
                                </div>
                            </dl>
                            <label className="check-row">
                                <input
                                    type="checkbox"
                                    checked={!!selected}
                                    onChange={(event) => {
                                        const checked = event.target.checked;
                                        change((current) => ({
                                            ...current,
                                            lines: checked
                                                ? [
                                                      ...current.lines,
                                                      {
                                                          source_line_id: source.source_line_id,
                                                          net_amount: "",
                                                          tax_amount: "",
                                                          recognition_date: current.credit_date,
                                                      },
                                                  ]
                                                : current.lines.filter(
                                                      (row) =>
                                                          row.source_line_id !==
                                                          source.source_line_id,
                                                  ),
                                        }));
                                    }}
                                />
                                {t("对此行登记贷项", "Credit this line")}
                            </label>
                            {selected && (
                                <>
                                    <div className="field">
                                        <label htmlFor={`${fieldId}-net`}>
                                            {t("本次贷项净额", "Credit net amount")}
                                        </label>
                                        <input
                                            id={`${fieldId}-net`}
                                            inputMode="decimal"
                                            value={selected.net_amount}
                                            onChange={(event) =>
                                                lineChange(
                                                    source.source_line_id,
                                                    "net_amount",
                                                    event.target.value,
                                                )
                                            }
                                        />
                                    </div>
                                    <div className="field">
                                        <label htmlFor={`${fieldId}-tax`}>
                                            {t("本次贷项税额", "Credit tax amount")}
                                        </label>
                                        <input
                                            id={`${fieldId}-tax`}
                                            inputMode="decimal"
                                            value={selected.tax_amount}
                                            onChange={(event) =>
                                                lineChange(
                                                    source.source_line_id,
                                                    "tax_amount",
                                                    event.target.value,
                                                )
                                            }
                                        />
                                    </div>
                                    <div className="field">
                                        <label htmlFor={`${fieldId}-recognition`}>
                                            {t("贷项归属日", "Credit recognition date")}
                                        </label>
                                        <input
                                            id={`${fieldId}-recognition`}
                                            type="date"
                                            value={selected.recognition_date}
                                            onChange={(event) =>
                                                lineChange(
                                                    source.source_line_id,
                                                    "recognition_date",
                                                    event.target.value,
                                                )
                                            }
                                        />
                                    </div>
                                </>
                            )}
                        </fieldset>
                    );
                })}
                <label className="check-row">
                    <input
                        type="checkbox"
                        checked={reviewed}
                        onChange={(event) =>
                            setReviewedKey(event.target.checked ? reviewKey : null)
                        }
                    />
                    {t(
                        "我已核对原分类、手填净额和税额，以及交易日和归属日；本次不自动退款。",
                        "I reviewed the original classification, manual net and tax, and both dates. This does not refund cash.",
                    )}
                </label>
                {invalid && (
                    <p role="alert">
                        {t(
                            "请选择原行，核对净额和税额、可贷含税额度及日期，然后勾选确认。",
                            "Select original lines, check net and tax amounts, available gross capacity and dates, then confirm the review.",
                        )}
                    </p>
                )}
            </fieldset>
            <div className="form-actions allocation-pagination">
                <button
                    className="primary-button"
                    type="submit"
                    disabled={disabled || !reviewed || body.lines.length === 0}
                >
                    {t("登记贷项", "Record credit")}
                </button>
                <button type="button" onClick={onCancel}>
                    {t("取消", "Cancel")}
                </button>
            </div>
        </form>
    );
}
