import type { Locale } from "./i18n";
import type { BusinessSettlementSnapshot } from "./pending-business-settlement";
import type { RefundFact } from "./refund-api";
import type { RefundRevisionInput, RefundRevisionReview } from "./refund-revision-intent";
import type { DeepReadonly } from "./settlement-intent";
import { amountFromMinorUnits, formatAmount } from "./money";
import { storedRefundUnits } from "./refund-revision-read";
import { SettlementRevisionOutcome } from "./SettlementRevisionOutcome";

export function RefundFactView({
    fact,
    assets,
    locale,
}: {
    fact: DeepReadonly<RefundFact>;
    assets: RefundRevisionReview["assets"];
    locale: Locale;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const display = (quantity: string, assetId: string) => {
        const units = storedRefundUnits(quantity, assetId, assets);
        return formatAmount(
            amountFromMinorUnits(units, assets.find((a) => a.asset_id === assetId)!.scale),
            locale,
        );
    };
    return (
        <div>
            <p>
                {t("现金", "Cash")} {display(fact.amount, fact.asset_id)} {fact.asset_id} ·{" "}
                {t("付款本金", "Payment principal")}{" "}
                {display(fact.payment_amount, fact.payment_asset_id)} {fact.payment_asset_id}
            </p>
            <p>
                {fact.transaction_date} · {fact.reason}
            </p>
            <details>
                <summary>
                    {t("账户、手续费及退回证据", "Account, fees and return evidence")}
                </summary>
                <p>
                    {t("现金账户", "Cash account")} {fact.account_id}
                </p>
                <p>
                    {t("手续费", "Fees")} ({fact.fees.length})
                </p>
                <ol>
                    {fact.fees.map((f, i) => (
                        <li key={i}>
                            {display(f.amount, f.asset_id)} {f.asset_id} · {f.account_id} ·{" "}
                            {f.category_id}
                        </li>
                    ))}
                </ol>
                <p>
                    {t("分摊退回证据", "Allocation return evidence")} (
                    {fact.allocation_return_ids.length})
                </p>
                <ol>
                    {fact.allocation_return_ids.map((id) => (
                        <li key={id}>{id}</li>
                    ))}
                </ol>
            </details>
        </div>
    );
}

export function BusinessRefundRevisionResult({
    pending,
    locale,
    onRetry,
    onClose,
}: {
    pending: BusinessSettlementSnapshot;
    locale: Locale;
    onRetry: () => void;
    onClose: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const plan = pending.plan;
    if (plan?.kind !== "refund-revision") return null;
    const body = JSON.parse(plan.bodyJson) as RefundRevisionInput["body"];
    const review = JSON.parse(plan.reviewedJson) as RefundRevisionReview;
    const receipt = pending.receipt?.kind === "business_refund_revision" ? pending.receipt : null;
    return (
        <section className="settlement-result">
            <h3>
                {plan.action === "correct"
                    ? t("更正退款", "Correct refund")
                    : t("取消退款", "Cancel refund")}
            </h3>
            <p>
                {t("退款", "Refund")} {plan.refundId} · {t("原版本", "Expected version")}{" "}
                {body.expected_version}
            </p>
            <p>{body.reason}</p>
            <h4>{t("修订前", "Before revision")}</h4>
            <RefundFactView
                assets={review.assets}
                locale={locale}
                fact={receipt?.previous ?? review.current.fact!}
            />
            {receipt?.replacement && (
                <>
                    <h4>{t("修订后", "After revision")}</h4>
                    <RefundFactView
                        assets={review.assets}
                        locale={locale}
                        fact={receipt.replacement}
                    />
                </>
            )}
            {!receipt && "replacement" in body && (
                <>
                    <h4>{t("提交的替代事实", "Submitted replacement")}</h4>
                    <RefundFactView
                        assets={review.assets}
                        locale={locale}
                        fact={{
                            ...body.replacement,
                            source_version: 1,
                            payment_asset_id: review.current.payment_asset_id,
                            recognition_date: body.replacement.transaction_date,
                            fees: body.replacement.fees ?? [],
                            allocation_return_ids: body.replacement.allocation_return_ids ?? [],
                        }}
                    />
                </>
            )}
            <SettlementRevisionOutcome
                pending={pending}
                receipt={receipt}
                locale={locale}
                onRetry={onRetry}
                onClose={onClose}
            />
        </section>
    );
}
