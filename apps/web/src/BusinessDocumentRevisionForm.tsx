import { useId, useState } from "react";
import type { DocumentCurrent } from "./document-revision-api";
import type { DocumentRevisionFields, ReviewedDocumentRevision } from "./document-revision-fields";
import { reviewDocumentRevision } from "./document-revision-fields";
import { validDocumentCurrent } from "./document-current-facts";
import type { DraftReferences, DraftLine } from "./draft-fields";
import { newDraftLine } from "./draft-fields";
import type { Locale } from "./i18n";
import { priceBusinessDocument } from "./business-pricing";
import { formatAmount } from "./money";
import { DraftLineFields } from "./DraftLineFields";
import { BusinessDocumentCurrentFacts } from "./BusinessDocumentCurrentFacts";
import "./draft-form.css";
import "./document-revision-form.css";

export function BusinessDocumentRevisionForm({
    current,
    fields,
    refs,
    locale,
    available,
    disabled,
    onChange,
    onSubmit,
    onBack,
}: {
    current: DocumentCurrent;
    fields: DocumentRevisionFields;
    refs: DraftReferences;
    locale: Locale;
    available: boolean;
    disabled: boolean;
    onChange: (update: (value: DocumentRevisionFields) => DocumentRevisionFields) => void;
    onSubmit: (value: ReviewedDocumentRevision) => void;
    onBack: () => void;
}) {
    const prefix = useId(),
        [reviewedKey, setReviewedKey] = useState<string | null>(null),
        [invalid, setInvalid] = useState(false);
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const oldAsset = refs.assets.find((a) => a.asset_id === current.document?.asset_id);
    if (!available)
        return <p role="alert">{t("请登录后重新读取单据。", "Sign in to reload the document.")}</p>;
    if (
        !validDocumentCurrent(current, current.ledger_id, current.document_id, oldAsset) ||
        current.status !== "active"
    )
        return (
            <p role="alert">
                {t(
                    "此单据不能修订，请重新读取当前版本。",
                    "This document cannot be revised. Reload its current version.",
                )}
            </p>
        );
    const candidate = reviewDocumentRevision(current, fields, refs),
        reviewKey = candidate ? JSON.stringify(candidate) : null;
    const reviewed = reviewKey !== null && reviewedKey === reviewKey;
    const replacement = fields.replacement,
        asset = refs.assets.find((a) => a.asset_id === replacement?.asset_id);
    let price = null;
    try {
        if (replacement && asset) price = priceBusinessDocument(replacement.lines, asset.scale);
    } catch {
        /* Preserve raw input for correction. */
    }
    const change = (update: (value: DocumentRevisionFields) => DocumentRevisionFields) => {
        setReviewedKey(null);
        setInvalid(false);
        onChange(update);
    };
    const updateReplacement = (
        update: (
            value: NonNullable<DocumentRevisionFields["replacement"]>,
        ) => NonNullable<DocumentRevisionFields["replacement"]>,
    ) => change((v) => (v.replacement ? { ...v, replacement: update(v.replacement) } : v));
    const updateLine = (line: DraftLine) =>
        updateReplacement((v) => ({
            ...v,
            lines: v.lines.map((r) => (r.id === line.id ? line : r)),
        }));
    const title =
        fields.action === "correct"
            ? t("更正整张单据", "Correct whole document")
            : t("取消整张单据", "Cancel whole document");
    return (
        <form
            className="document-revision-form"
            aria-label={title}
            onSubmit={(event) => {
                event.preventDefault();
                if (disabled) return;
                if (!candidate || !reviewed) {
                    setInvalid(true);
                    return;
                }
                onSubmit(candidate);
            }}
        >
            <h3>{title}</h3>
            <p>
                {t(
                    "原版和旧凭证会永久保留。更正替换整单，取消冲销全部当前应计；付款、退款和贷项不会自动修改。",
                    "Original versions and journals remain available. Correction replaces the whole document; cancellation reverses its current accruals. Payments, refunds and credits are not changed automatically.",
                )}
            </p>
            <BusinessDocumentCurrentFacts current={current} locale={locale} />
            <fieldset disabled={disabled} className="draft-fields">
                <legend>{t("修订原因", "Revision reason")}</legend>
                <label htmlFor={`${prefix}-reason`}>{t("原因（必填）", "Reason (required)")}</label>
                <textarea
                    id={`${prefix}-reason`}
                    required
                    maxLength={4000}
                    value={fields.reason}
                    onChange={(e) => change((v) => ({ ...v, reason: e.target.value }))}
                />
                {replacement && (
                    <>
                        <h4>{t("完整替代内容", "Complete replacement")}</h4>
                        <p>
                            {current.document_kind === "invoice"
                                ? t("销售 Invoice", "Sales invoice")
                                : t("采购账单", "Purchase bill")}
                        </p>
                        <div className="form-grid">
                            <div className="field">
                                <label htmlFor={`${prefix}-party`}>
                                    {t("往来单位", "Business party")}
                                </label>
                                <select
                                    id={`${prefix}-party`}
                                    required
                                    value={replacement.party_id}
                                    onChange={(e) =>
                                        updateReplacement((v) => ({
                                            ...v,
                                            party_id: e.target.value,
                                        }))
                                    }
                                >
                                    <option value="">{t("请选择", "Choose")}</option>
                                    {!refs.parties.some((r) => r.id === replacement.party_id) && (
                                        <option value={replacement.party_id} disabled>
                                            {replacement.party_id}
                                        </option>
                                    )}
                                    {refs.parties.map((r) => (
                                        <option
                                            key={r.id}
                                            value={r.id}
                                            disabled={
                                                r.archived ||
                                                ![
                                                    current.document_kind === "invoice"
                                                        ? "customer"
                                                        : "supplier",
                                                    "both",
                                                ].includes(r.role ?? "")
                                            }
                                        >
                                            {r.name}
                                            {r.archived ? ` · ${t("已归档", "Archived")}` : ""}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <div className="field">
                                <label htmlFor={`${prefix}-asset`}>
                                    {t("计价资产", "Pricing asset")}
                                </label>
                                <select
                                    id={`${prefix}-asset`}
                                    required
                                    value={replacement.asset_id}
                                    onChange={(e) =>
                                        updateReplacement((v) => ({
                                            ...v,
                                            asset_id: e.target.value,
                                        }))
                                    }
                                >
                                    <option value="">{t("请选择", "Choose")}</option>
                                    {!asset && (
                                        <option value={replacement.asset_id} disabled>
                                            {replacement.asset_id}
                                        </option>
                                    )}
                                    {refs.assets.map((r) => (
                                        <option
                                            key={r.asset_id}
                                            value={r.asset_id}
                                            disabled={!r.enabled}
                                        >
                                            {r.asset_id}
                                            {!r.enabled ? ` · ${t("已停用", "Disabled")}` : ""}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <div className="field">
                                <label htmlFor={`${prefix}-issue`}>
                                    {t("开单日期", "Issue date")}
                                </label>
                                <input
                                    id={`${prefix}-issue`}
                                    type="date"
                                    required
                                    min="0001-01-01"
                                    max="9999-12-31"
                                    value={replacement.issue_date}
                                    onChange={(e) =>
                                        updateReplacement((v) => ({
                                            ...v,
                                            issue_date: e.target.value,
                                        }))
                                    }
                                />
                            </div>
                            <div className="field">
                                <label htmlFor={`${prefix}-due`}>
                                    {t("到期日期（可选）", "Due date (optional)")}
                                </label>
                                <input
                                    id={`${prefix}-due`}
                                    type="date"
                                    min="0001-01-01"
                                    max="9999-12-31"
                                    value={replacement.due_date ?? ""}
                                    onChange={(e) =>
                                        updateReplacement((v) => ({
                                            ...v,
                                            due_date: e.target.value || null,
                                        }))
                                    }
                                />
                            </div>
                            <div className="field">
                                <label htmlFor={`${prefix}-notes`}>{t("备注", "Notes")}</label>
                                <textarea
                                    id={`${prefix}-notes`}
                                    maxLength={4000}
                                    value={replacement.notes ?? ""}
                                    onChange={(e) =>
                                        updateReplacement((v) => ({
                                            ...v,
                                            notes: e.target.value || null,
                                        }))
                                    }
                                />
                            </div>
                        </div>
                        <p className="help-text">
                            {t(
                                "每行先舍入未税净额，再计算税额。更换资产不会换算金额。保留的行沿用原身份；新增行获得新身份。",
                                "Each line rounds net before tax. Changing the asset does not convert amounts. Retained lines keep their identities; added lines get new identities.",
                            )}
                        </p>
                        {replacement.lines.map((line, index) => (
                            <DraftLineFields
                                key={line.id}
                                idPrefix={`${prefix}-line`}
                                value={line}
                                index={index}
                                kind={current.document_kind}
                                refs={refs}
                                locale={locale}
                                price={price?.lines[index]}
                                assetId={replacement.asset_id}
                                onChange={updateLine}
                                onRemove={() =>
                                    updateReplacement((v) => ({
                                        ...v,
                                        lines: v.lines.filter((r) => r.id !== line.id),
                                    }))
                                }
                            />
                        ))}
                        <button
                            type="button"
                            disabled={replacement.lines.length >= 200}
                            onClick={() =>
                                updateReplacement((v) => ({
                                    ...v,
                                    lines: [
                                        ...v.lines,
                                        newDraftLine(crypto.randomUUID(), v.issue_date),
                                    ],
                                }))
                            }
                        >
                            {t("添加明细行", "Add line")}
                        </button>
                        <label className="check-row">
                            <input
                                type="checkbox"
                                checked={fields.refreshReferences}
                                onChange={(e) =>
                                    change((v) => ({ ...v, refreshReferences: e.target.checked }))
                                }
                            />
                            {t(
                                "显式刷新所有选定往来、分类和项目的快照",
                                "Explicitly refresh all selected party, category and project snapshots",
                            )}
                        </label>
                        <p className="help-text">
                            {t(
                                "未刷新且未改变的引用保留各行旧快照。新增或更换的引用使用下列审阅版本；同一资料身份的所有替代行都会使用该版本。开票主体快照保留。",
                                "Unchanged, unrefreshed references retain each line's historical snapshot. New or changed references use the reviewed versions below, shared by every replacement line with that identity. The issuer snapshot is retained.",
                            )}
                        </p>
                    </>
                )}
            </fieldset>
            {replacement && (
                <div className="draft-totals" aria-live="polite">
                    {price ? (
                        <dl>
                            {(
                                [
                                    ["net_amount", "未税合计", "Net total"],
                                    ["tax_amount", "税额合计", "Tax total"],
                                    ["total_amount", "含税合计", "Total including tax"],
                                ] as const
                            ).map(([key, zh, en]) => (
                                <div key={key}>
                                    <dt>{t(zh, en)}</dt>
                                    <dd>
                                        {formatAmount(price[key], locale)} {replacement.asset_id}
                                    </dd>
                                </div>
                            ))}
                        </dl>
                    ) : (
                        <p>
                            {t(
                                "请核对资产及各行精确数量、单价、折扣和税率。",
                                "Check the asset and each line's exact quantity, price, discount and tax rate.",
                            )}
                        </p>
                    )}
                </div>
            )}
            {candidate && (
                <section aria-label={t("资料审阅版本", "Reviewed reference versions")}>
                    {candidate.review.references.length ? (
                        <ul>
                            {candidate.review.references.map(({ kind, record }) => (
                                <li key={`${kind}:${record.id}`}>
                                    {kind === "party"
                                        ? t("往来单位", "Business party")
                                        : kind === "category"
                                          ? t("分类", "Category")
                                          : t("项目", "Project")}
                                    : {record.name} · {t("版本", "Version")} {record.version}
                                </li>
                            ))}
                        </ul>
                    ) : (
                        <p>
                            {t(
                                "此修订不刷新已有资料快照。",
                                "This revision does not refresh existing reference snapshots.",
                            )}
                        </p>
                    )}
                </section>
            )}
            <label className="check-row">
                <input
                    type="checkbox"
                    checked={reviewed}
                    disabled={disabled || !candidate}
                    onChange={(e) => setReviewedKey(e.target.checked ? reviewKey : null)}
                />
                {t(
                    "我已核对当前单据、完整替代内容或取消范围，以及上列资料版本。",
                    "I reviewed the current document, the complete replacement or cancellation, and the reference versions above.",
                )}
            </label>
            {invalid && (
                <p role="alert">
                    {t(
                        "请填写有效原因，核对完整内容并确认审阅。",
                        "Enter a valid reason, check the complete contents and confirm your review.",
                    )}
                </p>
            )}
            <div className="form-actions">
                <button type="submit" disabled={disabled || !candidate || !reviewed}>
                    {fields.action === "correct"
                        ? t("提交整单更正", "Submit whole-document correction")
                        : t("提交整单取消", "Submit whole-document cancellation")}
                </button>
                <button type="button" disabled={disabled} onClick={onBack}>
                    {t("返回", "Back")}
                </button>
            </div>
        </form>
    );
}
