import { useId, useState } from "react";
import type { Category } from "./ledger-api";
import type { Locale } from "./i18n";
import type { CreditRevisionReview } from "./credit-revision-intent";
import { stripCreditRevisionReason } from "./credit-revision-intent";
import type { CreditRevisionDraft } from "./credit-revision-fields";
import {
    canSubmitCreditRevision,
    creditRevisionLimits,
    formatCreditRevisionAmount,
    selectCreditRevisionLine,
    setCreditRevisionReason,
    updateCreditCorrection,
} from "./credit-revision-fields";
import "./draft-form.css";
import "./credit-form.css";

export function BusinessCreditRevisionForm({
    ledgerId,
    creditId,
    locale,
    draft,
    review,
    categories,
    disabled,
    onChange,
    onSubmit,
    onBack,
}: {
    ledgerId: string;
    creditId: string;
    locale: Locale;
    draft: CreditRevisionDraft;
    review: CreditRevisionReview;
    categories: Category[];
    disabled: boolean;
    onChange: (update: (current: CreditRevisionDraft) => CreditRevisionDraft) => void;
    onSubmit: (draft: CreditRevisionDraft, review: CreditRevisionReview) => void;
    onBack: () => void;
}) {
    const prefix = useId();
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const { current, asset } = review;
    const replacement = draft.action === "correct" ? draft.body.replacement : null;
    const blocked = disabled || current.status !== "active" || (!!replacement && !asset.enabled);
    const reviewKey = JSON.stringify({ ledgerId, creditId, draft, review });
    const [reviewedKey, setReviewedKey] = useState<string | null>(null);
    const [invalid, setInvalid] = useState(false);
    const reviewed = reviewedKey === reviewKey;
    const amount = (value: string) =>
        formatCreditRevisionAmount(value, asset, locale) ??
        t("无效金额，请重新读取", "Invalid amount; reload the snapshot");
    function change(update: (current: CreditRevisionDraft) => CreditRevisionDraft) {
        setReviewedKey(null);
        setInvalid(false);
        onChange(update);
    }
    function reasonChange(value: string) {
        change((state) => setCreditRevisionReason(state, value));
    }
    function replacementChange(field: "credit_date" | "reason", value: string) {
        change((state) =>
            updateCreditCorrection(state, (body) => ({
                ...body,
                replacement: { ...body.replacement, [field]: value },
            })),
        );
    }
    function selectLine(sourceId: string, selected: boolean) {
        change((state) => selectCreditRevisionLine(state, review, sourceId, selected));
    }
    function lineChange(
        sourceId: string,
        field: "net_amount" | "tax_amount" | "recognition_date",
        value: string,
    ) {
        change((state) =>
            updateCreditCorrection(state, (body) => ({
                ...body,
                replacement: {
                    ...body.replacement,
                    lines: body.replacement.lines.map((line) =>
                        line.source_line_id === sourceId ? { ...line, [field]: value } : line,
                    ),
                },
            })),
        );
    }
    const title = replacement
        ? t("更正贷项组", "Correct credit group")
        : t("取消贷项组", "Cancel credit group");
    return (
        <form
            className="credit-form"
            aria-label={title}
            noValidate
            onSubmit={(event) => {
                event.preventDefault();
                if (
                    blocked ||
                    !reviewed ||
                    !canSubmitCreditRevision(ledgerId, creditId, draft, review)
                ) {
                    setInvalid(true);
                    return;
                }
                onSubmit(draft, review);
            }}
        >
            <h3>{title}</h3>
            <p className="help-text">
                {t(
                    "这是对整组原事实的错误更正或取消，不自动创建退款、退回或分摊。原记录和回执永久保留。",
                    "This corrects or cancels the complete credit group. It does not create refunds, returns or allocations. Original records and receipts remain permanent.",
                )}
            </p>
            <section aria-label={t("当前贷项事实", "Current credit facts")}>
                <p>
                    {t("当前组版本", "Current group version")}: {current.version} ·{" "}
                    {current.credit_date}
                </p>
                <p>{current.credit_reason}</p>
                <ul className="master-records">
                    {current.members.map((row) => (
                        <li key={row.source_line_id}>
                            <strong>
                                {review.capacity?.lines.find(
                                    (source) => source.source_line_id === row.source_line_id,
                                )?.description ?? row.source_line_id}
                            </strong>
                            <span>
                                {t("旧净额 / 税额 / 含税额", "Previous net / tax / gross")}:{" "}
                                {amount(row.net_amount)} / {amount(row.tax_amount)} /{" "}
                                {amount(row.total_amount)}
                            </span>
                            <span>
                                {t("旧归属日", "Previous recognition date")}: {row.recognition_date}
                            </span>
                        </li>
                    ))}
                </ul>
            </section>
            <fieldset disabled={blocked}>
                <legend>{title}</legend>
                <div className="field">
                    <label htmlFor={`${prefix}-reason`}>
                        {t("本次更正或取消的原因", "Reason for this correction or cancellation")}
                    </label>
                    <textarea
                        id={`${prefix}-reason`}
                        value={draft.body.reason}
                        onChange={(event) => reasonChange(event.target.value)}
                    />
                    <p className="help-text">
                        {t("去除首尾空白后的字符数", "Characters after trimming whitespace")}:{" "}
                        {[...stripCreditRevisionReason(draft.body.reason)].length} / 1000
                    </p>
                </div>
                {replacement ? (
                    <>
                        <div className="field">
                            <label htmlFor={`${prefix}-date`}>
                                {t("替代贷项交易日", "Replacement credit transaction date")}
                            </label>
                            <input
                                id={`${prefix}-date`}
                                type="date"
                                min={review.capacity?.issue_date}
                                value={replacement.credit_date}
                                onChange={(event) =>
                                    replacementChange("credit_date", event.target.value)
                                }
                            />
                        </div>
                        <div className="field">
                            <label htmlFor={`${prefix}-fact-reason`}>
                                {t("替代贷项说明", "Replacement credit reason")}
                            </label>
                            <textarea
                                id={`${prefix}-fact-reason`}
                                value={replacement.reason}
                                onChange={(event) =>
                                    replacementChange("reason", event.target.value)
                                }
                            />
                            <p className="help-text">
                                {t("字符数（包含首尾空白）", "Characters including whitespace")}:{" "}
                                {[...replacement.reason].length} / 2000
                            </p>
                        </div>
                        <p className="help-text">
                            {t(
                                "提交全部替代行，至少保留一行。未选中的旧成员将移除并冲销；保留成员即使金额不变也会完整冲销再替代。删除整组请使用取消。",
                                "Submit every replacement line, with at least one selected. Omitted existing members are reversed and removed. Retained members are fully reversed and replaced even when amounts are unchanged. Use cancellation to remove the entire group.",
                            )}
                        </p>
                        {!review.capacity && (
                            <p role="alert">
                                {t(
                                    "缺少完整容量快照，请重新读取。",
                                    "Reload the complete capacity snapshot.",
                                )}
                            </p>
                        )}
                        {review.capacity?.lines.map((source) => {
                            const selected = replacement.lines.find(
                                (row) => row.source_line_id === source.source_line_id,
                            );
                            const previous = current.members.find(
                                (row) => row.source_line_id === source.source_line_id,
                            );
                            const limits = creditRevisionLimits(review, source.source_line_id);
                            const category = categories.find(
                                (row) => row.id === source.category_id,
                            );
                            const fieldId = `${prefix}-${source.source_line_id}`;
                            return (
                                <fieldset key={source.source_line_id} className="draft-line">
                                    <legend>{source.description}</legend>
                                    <p>
                                        {t("原分类", "Original category")}:{" "}
                                        {category?.name ?? source.category_id}
                                    </p>
                                    <p>
                                        {t(
                                            "排除本组后的净额 / 税额 / 含税限额",
                                            "Net / tax / gross limits excluding this group",
                                        )}
                                        :{" "}
                                        {limits
                                            ? `${amount(limits.net)} / ${amount(limits.tax)} / ${amount(limits.gross)}`
                                            : t(
                                                  "无法核对，请重新读取",
                                                  "Cannot verify; reload the snapshot",
                                              )}
                                    </p>
                                    <label className="check-row">
                                        <input
                                            type="checkbox"
                                            checked={!!selected}
                                            onChange={(event) =>
                                                selectLine(
                                                    source.source_line_id,
                                                    event.target.checked,
                                                )
                                            }
                                        />
                                        {t(
                                            "纳入完整替代内容",
                                            "Include in the complete replacement",
                                        )}
                                    </label>
                                    {previous && !selected && (
                                        <p>
                                            {t(
                                                "提交后移除并冲销此旧成员。",
                                                "This existing member will be removed and reversed.",
                                            )}
                                        </p>
                                    )}
                                    {selected && (
                                        <>
                                            <p>
                                                {t("替代顺序", "Replacement position")}:{" "}
                                                {replacement.lines.indexOf(selected) + 1}
                                            </p>
                                            {(
                                                [
                                                    ["net_amount", "替代净额", "Replacement net"],
                                                    ["tax_amount", "替代税额", "Replacement tax"],
                                                ] as const
                                            ).map(([field, zh, en]) => (
                                                <div className="field" key={field}>
                                                    <label htmlFor={`${fieldId}-${field}`}>
                                                        {t(zh, en)}
                                                    </label>
                                                    <input
                                                        id={`${fieldId}-${field}`}
                                                        inputMode="decimal"
                                                        value={selected[field]}
                                                        onChange={(event) =>
                                                            lineChange(
                                                                source.source_line_id,
                                                                field,
                                                                event.target.value,
                                                            )
                                                        }
                                                    />
                                                </div>
                                            ))}
                                            <div className="field">
                                                <label htmlFor={`${fieldId}-recognition`}>
                                                    {t(
                                                        "替代归属日",
                                                        "Replacement recognition date",
                                                    )}
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
                        <p className="help-text">
                            {t(
                                "容量是审阅快照，提交时会重新核对；旧日期和所有替代日期都须处于开放期间。",
                                "Capacity is a review snapshot and is checked again on submission. All previous and replacement dates must be in open periods.",
                            )}
                        </p>
                    </>
                ) : (
                    <p className="help-text">
                        {t(
                            "将按全部旧日期和维度精确冲销上述所有成员。旧资产停用不妨碍取消；已结账期间须先带理由重开。取消后不能恢复此组。",
                            "Every member above will be reversed with its exact previous dates and dimensions. A disabled asset does not prevent cancellation; closed periods must first be reopened with a reason. A cancelled group cannot be reactivated.",
                        )}
                    </p>
                )}
                <label className="check-row">
                    <input
                        type="checkbox"
                        checked={reviewed}
                        onChange={(event) =>
                            setReviewedKey(event.target.checked ? reviewKey : null)
                        }
                    />
                    {t(
                        "我已核对整组旧事实、本次原因、完整替代或取消内容及日期；本次不自动退款。",
                        "I reviewed the complete previous group, reason, full replacement or cancellation and dates. This does not refund cash.",
                    )}
                </label>
                {invalid && (
                    <p role="alert">
                        {t(
                            "请核对组版本、原因、完整行、手填净额/税额、容量及日期，然后重新确认。原输入已保留。",
                            "Check the group version, reason, complete lines, manual net/tax, capacity and dates, then confirm the review. Your input is preserved.",
                        )}
                    </p>
                )}
            </fieldset>
            <div className="form-actions allocation-pagination">
                <button className="primary-button" type="submit" disabled={blocked || !reviewed}>
                    {replacement
                        ? t("确认更正", "Confirm correction")
                        : t("确认取消贷项", "Confirm credit cancellation")}
                </button>
                <button type="button" onClick={onBack} disabled={disabled}>
                    {t("返回", "Back")}
                </button>
            </div>
        </form>
    );
}
