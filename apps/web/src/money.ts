/** Exact quantity helpers. Asset scales, pagination and dates are not quantities. */
export type AmountErrorCode = "amount_format" | "amount_precision" | "amount_range" | "asset_scale";
export class AmountError extends Error {
    readonly code: AmountErrorCode;
    constructor(code: AmountErrorCode) {
        super(code);
        this.name = "AmountError";
        this.code = code;
    }
}

function checkScale(scale: number): void {
    if (!Number.isInteger(scale) || scale < 0 || scale > 18) throw new AmountError("asset_scale");
}

function parts(amount: string): { negative: boolean; integer: string; fraction: string } {
    if (typeof amount !== "string" || amount.length > 40) throw new AmountError("amount_range");
    if (!/^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/u.test(amount)) throw new AmountError("amount_format");
    const negative = amount.startsWith("-");
    const [integer, fraction = ""] = (negative ? amount.slice(1) : amount).split(".");
    if (integer.length > 20) throw new AmountError("amount_range");
    if (fraction.length > 18) throw new AmountError("amount_precision");
    return { negative, integer, fraction };
}

/** Strict API decimal syntax; extra trailing zeroes still exceed an asset's scale. */
export function parseAmount(amount: string, scale: number): bigint {
    checkScale(scale);
    const { negative, integer, fraction } = parts(amount);
    if (fraction.length > scale) throw new AmountError("amount_precision");
    const units = BigInt(integer + fraction.padEnd(scale, "0"));
    return negative ? -units : units;
}

export function amountFromMinorUnits(units: bigint, scale: number): string {
    checkScale(scale);
    if (typeof units !== "bigint") throw new AmountError("amount_format");
    const negative = units < 0n;
    const absolute = negative ? -units : units;
    if (absolute >= 10n ** BigInt(20 + scale)) throw new AmountError("amount_range");
    const digits = absolute.toString().padStart(scale + 1, "0");
    const value = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
    return negative ? `-${value}` : value;
}

/** Final net totals use integers, so intermediate cancellation cannot round or overflow. */
export function sumAmounts(amounts: readonly string[], scale: number): string {
    return amountFromMinorUnits(
        amounts.reduce((total, amount) => total + parseAmount(amount, scale), 0n),
        scale,
    );
}

/** Keep every original fractional digit; Intl only formats the integer BigInt. */
export function formatAmount(amount: string, locale = "en"): string {
    const { negative, integer, fraction } = parts(amount);
    const language = locale === "zh" ? "zh-CN" : locale;
    const grouped = new Intl.NumberFormat(language, { maximumFractionDigits: 0 }).format(
        BigInt(integer),
    );
    const decimal =
        new Intl.NumberFormat(language, { minimumFractionDigits: 1 })
            .formatToParts(0n)
            .find((part) => part.type === "decimal")?.value ?? ".";
    return `${negative ? "-" : ""}${grouped}${fraction ? decimal + fraction : ""}`;
}
