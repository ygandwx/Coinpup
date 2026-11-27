import type { Locale } from "./i18n";
import { formatAssetAmount } from "./money";

export function DisplayAmount({
    amount,
    assetId,
    locale,
    expense = false,
}: {
    amount: string;
    assetId: string;
    locale: Locale;
    expense?: boolean;
}) {
    return (
        <span className="ui-amount" data-negative={expense || amount.startsWith("-")}>
            {formatAssetAmount(amount, assetId, locale)}
        </span>
    );
}
