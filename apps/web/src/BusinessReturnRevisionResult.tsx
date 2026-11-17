import type { Locale } from "./i18n";
import type { BusinessSettlementSnapshot } from "./pending-business-settlement";
import type {
    ReturnRevisionInput,
    ReturnRevisionReview,
} from "./allocation-return-revision-intent";
import { reviewedSources } from "./allocation-return-revision-intent";
import { ReturnRevisionFacts } from "./BusinessReturnRevisionHistory";
import { SettlementRevisionOutcome } from "./SettlementRevisionOutcome";
import { formatAmount } from "./money";

export function BusinessReturnRevisionResult({
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
    const plan = pending.plan;
    if (plan?.kind !== "return-revision") return null;
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const review = JSON.parse(plan.reviewedJson) as ReturnRevisionReview;
    const body = JSON.parse(plan.bodyJson) as ReturnRevisionInput["body"];
    const receipt =
        pending.receipt?.kind === "business_allocation_return_revision" ? pending.receipt : null;
    const sources = reviewedSources("replacement" in body && !receipt ? review.capacities : []);
    return (
        <section className="settlement-result">
            <h3>
                {plan.action === "correct"
                    ? t("更正分摊退回", "Correct allocation return")
                    : t("取消分摊退回", "Cancel allocation return")}
            </h3>
            <p>
                {t("退回组", "Return group")} {plan.returnId} · {t("原版本", "Expected version")}{" "}
                {body.expected_version}
            </p>
            <p>{body.reason}</p>
            {receipt ? (
                <ReturnRevisionFacts receipt={receipt} assets={review.assets} locale={locale} />
            ) : (
                <>
                    <p>
                        {review.current.fact!.return_date} · {review.current.fact!.reason}
                    </p>
                    {"replacement" in body && (
                        <>
                            <h4>{t("提交的替代退回", "Submitted replacement return")}</h4>
                            <p>
                                {body.replacement.return_date} · {body.replacement.reason}
                            </p>
                            <ol>
                                {body.replacement.lines.map((line) => {
                                    const source = sources.get(
                                        line.source_allocation_line_id,
                                    )!.source;
                                    return (
                                        <li key={line.source_allocation_line_id}>
                                            {source.description} · {line.source_allocation_line_id}
                                            <p>
                                                {formatAmount(line.payment_amount, locale)}{" "}
                                                {review.current.payment_asset_id} /{" "}
                                                {formatAmount(line.document_amount, locale)}{" "}
                                                {source.document_asset_id}
                                            </p>
                                        </li>
                                    );
                                })}
                            </ol>
                        </>
                    )}
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
