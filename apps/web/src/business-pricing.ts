import type { components } from "./generated/openapi";
import { amountFromMinorUnits, parseAmount, sumAmounts } from "./money";

type Source = components["schemas"]["BusinessDraftLineInput"];
export type PricingInput = Pick<
    Source,
    "quantity" | "unit_price" | "discount_amount" | "tax_rate_percent"
>;
export type LinePrice = Pick<
    components["schemas"]["BusinessDraftLineResponse"],
    "net_amount" | "tax_amount" | "total_amount"
>;
export type DocumentPrice = LinePrice & { lines: LinePrice[] };

export class BusinessPricingError extends Error {
    readonly code: string;
    constructor(code: string) {
        super(code);
        this.name = "BusinessPricingError";
        this.code = code;
    }
}

function ratio(value: unknown, code: string, positive = false): [bigint, bigint] {
    if (
        typeof value !== "string" ||
        value.length > 39 ||
        !/^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$/u.test(value)
    ) {
        throw new BusinessPricingError(code);
    }
    const [integer, fraction = ""] = value.split(".");
    const numerator = BigInt(integer + fraction);
    if (positive && numerator === 0n) throw new BusinessPricingError(code);
    return [numerator, 10n ** BigInt(fraction.length)];
}

function money(value: unknown, scale: number): bigint {
    if (typeof value !== "string") throw new BusinessPricingError("amount_type");
    return parseAmount(value, scale);
}

function halfUp(numerator: bigint, denominator: bigint): bigint {
    return numerator / denominator + (2n * (numerator % denominator) >= denominator ? 1n : 0n);
}

/** ADR0026: net after fixed line discount, then tax on rounded net. Never alter source text. */
export function priceBusinessLine(value: PricingInput, scale: number): LinePrice {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new BusinessPricingError("pricing_line");
    }
    const [quantity, quantityScale] = ratio(value.quantity, "pricing_quantity", true);
    const [rate, rateScale] = ratio(
        value.tax_rate_percent === undefined ? "0" : value.tax_rate_percent,
        "pricing_tax_rate",
    );
    const price = money(value.unit_price, scale);
    const discount = money(
        value.discount_amount === undefined ? "0" : value.discount_amount,
        scale,
    );
    if (price < 0n) throw new BusinessPricingError("pricing_price");
    if (discount < 0n) throw new BusinessPricingError("pricing_discount");
    const discounted = quantity * price - discount * quantityScale;
    if (discounted < 0n) throw new BusinessPricingError("pricing_discount");
    const net = halfUp(discounted, quantityScale);
    const tax = halfUp(net * rate, rateScale * 100n);
    return {
        net_amount: amountFromMinorUnits(net, scale),
        tax_amount: amountFromMinorUnits(tax, scale),
        total_amount: amountFromMinorUnits(net + tax, scale),
    };
}

export function priceBusinessDocument(
    values: readonly PricingInput[],
    scale: number,
): DocumentPrice {
    if (!Array.isArray(values) || values.length > 200)
        throw new BusinessPricingError("pricing_lines");
    amountFromMinorUnits(0n, scale); // Validate the selected asset precision even for an empty draft.
    const lines = values.map((value) => priceBusinessLine(value, scale));
    return {
        lines,
        net_amount: sumAmounts(
            lines.map((line) => line.net_amount),
            scale,
        ),
        tax_amount: sumAmounts(
            lines.map((line) => line.tax_amount),
            scale,
        ),
        total_amount: sumAmounts(
            lines.map((line) => line.total_amount),
            scale,
        ),
    };
}
