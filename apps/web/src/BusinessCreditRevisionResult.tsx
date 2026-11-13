import type { ApiError } from "./api";
import type { CreditCorrection, CreditRevisionReceipt } from "./credit-api";
import type { CreditRevisionReview, FrozenCreditRevision } from "./credit-revision-intent";
import { validCreditRevisionReview } from "./credit-revision-intent";
import { validCreditRevisionReceipt } from "./credit-revision-receipt";
import { creditRevisionError } from "./credit-revision-errors";
import type { PendingStatus } from "./pending-command";
import type { DeepReadonly } from "./settlement-intent";
import type { Locale } from "./i18n";
import { formatAmount, sumAmounts } from "./money";
import "./credit-form.css";

export type BusinessCreditRevisionResultProps = {
    plan: FrozenCreditRevision;
    status: PendingStatus;
    revisionReceipt: DeepReadonly<CreditRevisionReceipt> | null;
    error: ApiError | null;
    locale: Locale;
    onRetry: () => void;
    onEdit: () => void;
    onClose: () => void;
};

function read(props: BusinessCreditRevisionResultProps) {
    try {
        const body = JSON.parse(props.plan.bodyJson) as CreditCorrection;
        const review = JSON.parse(props.plan.reviewedJson) as CreditRevisionReview;
        if (
            !validCreditRevisionReview(
                props.plan.ledgerId,
                props.plan.creditId,
                props.plan.action,
                body,
                review,
            ) ||
            (props.status === "confirmed" &&
                !validCreditRevisionReceipt(props.revisionReceipt, props.plan))
        )
            return null;
        return { body, review };
    } catch {
        return null;
    }
}

/** A frozen command and its particular receipt, never a substitute for the current group. */
export function BusinessCreditRevisionResult(props: BusinessCreditRevisionResultProps) {
    const { plan, status, locale, onRetry, onEdit, onClose } = props;
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    if (status === "idle") return null;
    if (status === "auth-required")
        return (
            <p role="alert">
                {t(
                    "请以原用户登录后手动恢复本次修订。",
                    "Sign in as the original user to manually resume this revision.",
                )}
            </p>
        );
    const data = read(props);
    if (!data)
        return (
            <p role="alert">
                {t(
                    "无法安全显示本次修订。请保留此页及原请求，核对服务端记录后再处理。",
                    "This revision cannot be displayed safely. Preserve this page and the original request while checking the server records.",
                )}
            </p>
        );
    const { body, review } = data,
        current = review.current;
    const saved = status === "confirmed" ? props.revisionReceipt! : null;
    const replacement = plan.action === "correct" ? body.replacement : null;
    const lines = replacement?.lines ?? [];
    const rows = [
        ...lines.map((line) => ({
            line,
            old: current.members.find((row) => row.source_line_id === line.source_line_id),
        })),
        ...current.members
            .filter((row) => !lines.some((line) => line.source_line_id === row.source_line_id))
            .map((old) => ({ line: undefined, old })),
    ];
    const money = (amount: string) => `${formatAmount(amount, locale)} ${review.asset.asset_id}`;
    const fact = (title: string, net: string, tax: string, total: string, date: string) => (
        <div>
            <strong>{title}</strong>
            <span>
                {t("净额", "Net")}: {money(net)}
            </span>
            <span>
                {t("税额", "Tax")}: {money(tax)}
            </span>
            <span>
                {t("含税额", "Gross")}: {money(total)}
            </span>
            <span>
                {t("归属日", "Recognition date")}: {date}
            </span>
        </div>
    );
    return (
        <section className="credit-form" aria-label={t("本次贷项修订", "This credit revision")}>
            <h3>
                {plan.action === "correct"
                    ? t("贷项整组更正", "Complete credit correction")
                    : t("贷项整组取消", "Complete credit cancellation")}
            </h3>
            <p>
                {t(
                    "以下是提交时冻结的内容。本次回执只证明这一次修订；当前状态可能已有后续版本。",
                    "These facts were frozen for this submission. This receipt proves only this revision; the current group may have later versions.",
                )}
            </p>
            <p>
                {t(
                    "修订只调整贷项事实，不自动产生退款或其他现金流。",
                    "The revision changes credit facts without automatically refunding cash or recording other cash movements.",
                )}
            </p>
            <dl className="confirmation-facts">
                <dt>{t("贷项组标识", "Credit group ID")}</dt>
                <dd>{plan.creditId}</dd>
                <dt>{t("原单据标识", "Original document ID")}</dt>
                <dd>{current.document_id}</dd>
                <dt>{t("前版组版本", "Previous group version")}</dt>
                <dd>{current.version}</dd>
                <dt>
                    {saved
                        ? t("本次组版本", "This revision's group version")
                        : t("预期组版本", "Expected group version")}
                </dt>
                <dd>{current.version + 1}</dd>
                <dt>{t("独立修订原因", "Revision reason")}</dt>
                <dd>{saved ? saved.reason : body.reason}</dd>
                <dt>{t("前版贷项交易日", "Previous credit date")}</dt>
                <dd>{current.credit_date}</dd>
                <dt>{t("前版事实说明", "Previous fact reason")}</dt>
                <dd>{current.credit_reason}</dd>
                {replacement && (
                    <>
                        <dt>{t("替代贷项交易日", "Replacement credit date")}</dt>
                        <dd>{saved ? saved.credit_date : replacement.credit_date}</dd>
                        <dt>{t("替代事实说明", "Replacement fact reason")}</dt>
                        <dd>{saved ? saved.replacement_reason : replacement.reason}</dd>
                    </>
                )}
                {saved && (
                    <>
                        <dt>{t("本次修订回执标识", "This revision receipt ID")}</dt>
                        <dd>{saved.revision_id}</dd>
                    </>
                )}
            </dl>
            <ul className="master-records">
                {rows.map(({ old, line }, index) => {
                    const sourceId = line?.source_line_id ?? old!.source_line_id;
                    const source = review.confirmation.postings.find(
                        (row) => row.line_id === sourceId,
                    )!;
                    const member = saved?.members[index];
                    return (
                        <li key={sourceId}>
                            <h4>
                                {t("原单据行", "Original document line")} {source.line_no} ·{" "}
                                {old
                                    ? line
                                        ? t("保留并更正", "Retained and corrected")
                                        : t("移除", "Removed")
                                    : t("新增", "Added")}
                            </h4>
                            <span>
                                {t("源行标识", "Source line ID")}: {sourceId}
                            </span>
                            {old ? (
                                <>
                                    <span>
                                        {t("前版行操作版本", "Previous member operation version")}:{" "}
                                        {old.operation_version}
                                    </span>
                                    {fact(
                                        t("前版", "Before"),
                                        member?.previous_net_amount ?? old.net_amount,
                                        member?.previous_tax_amount ?? old.tax_amount,
                                        member?.previous_total_amount ?? old.total_amount,
                                        old.recognition_date,
                                    )}
                                </>
                            ) : (
                                <p>
                                    {t(
                                        "此前无有效成员，本次新增使用新的行操作。",
                                        "There was no effective member here; this addition uses a new member operation.",
                                    )}
                                </p>
                            )}
                            <span>
                                {saved
                                    ? t(
                                          "本次行操作版本",
                                          "This revision's member operation version",
                                      )
                                    : t("预期行操作版本", "Expected member operation version")}
                                : {old ? old.operation_version + 1 : 1}
                            </span>
                            {line ? (
                                fact(
                                    t("本次替代", "Replacement"),
                                    member?.net_amount ?? line.net_amount,
                                    member?.tax_amount ?? line.tax_amount,
                                    member?.total_amount ??
                                        sumAmounts(
                                            [line.net_amount, line.tax_amount],
                                            review.asset.scale,
                                        ),
                                    line.recognition_date,
                                )
                            ) : (
                                <p>
                                    {t(
                                        "移除此行，仅冲销前版，不写入零金额替代。",
                                        "This member is removed by reversing its previous fact; no zero-amount replacement is recorded.",
                                    )}
                                </p>
                            )}
                            {member && (
                                <details>
                                    <summary>
                                        {t("本次凭证标识", "Posting identities for this revision")}
                                    </summary>
                                    <span>
                                        {t("行操作", "Member operation")}: {member.operation_id}
                                    </span>
                                    {member.previous_journal_id && (
                                        <span>
                                            {t("前版凭证", "Previous posting")}:{" "}
                                            {member.previous_journal_id}
                                        </span>
                                    )}
                                    {member.reversal_journal_id && (
                                        <span>
                                            {t("冲销凭证", "Reversal")}:{" "}
                                            {member.reversal_journal_id}
                                        </span>
                                    )}
                                    {member.journal_id && (
                                        <span>
                                            {t("替代凭证", "Replacement posting")}:{" "}
                                            {member.journal_id}
                                        </span>
                                    )}
                                </details>
                            )}
                        </li>
                    );
                })}
            </ul>
            {status === "submitting" && (
                <p role="status">
                    {t("正在提交冻结的原意图…", "Submitting the frozen original intent…")}
                </p>
            )}
            {status === "unknown" && (
                <p role="alert">
                    {t(
                        "结果尚未确认。请保留此页并重试原请求，不按当前余额或历史列表另外提交。刷新或关闭页面会丢失内存中的意图。",
                        "The outcome is unknown. Keep this page and retry the original request; do not submit another from current balances or history. Refreshing or closing loses the in-memory intent.",
                    )}
                </p>
            )}
            {status === "idempotency-conflict" && (
                <p role="alert">
                    {t(
                        "原幂等键与已保存请求不符。请保留此页面核对，不能自动换键或丢弃此意图。",
                        "The original key conflicts with a saved request. Preserve this page for investigation; the key or intent cannot be replaced automatically.",
                    )}
                </p>
            )}
            {status === "rejected" && (
                <p role="alert">{creditRevisionError(props.error, locale)}</p>
            )}
            {saved && (
                <p role="status">
                    {t(
                        "本次修订已保存，完整过渡回执已核对。此回执不代表今日的当前状态。",
                        "This revision is saved and its complete transition receipt is verified. This receipt does not represent today's current state.",
                    )}
                </p>
            )}
            <div className="form-actions">
                {status === "unknown" && (
                    <button type="button" onClick={onRetry}>
                        {t("重试原请求", "Retry original request")}
                    </button>
                )}
                {status === "rejected" && (
                    <button type="button" onClick={onEdit}>
                        {t("修改被拒绝的请求", "Edit rejected request")}
                    </button>
                )}
                {saved && (
                    <button type="button" onClick={onClose}>
                        {t("关闭本次修订回执", "Close this revision receipt")}
                    </button>
                )}
            </div>
        </section>
    );
}
