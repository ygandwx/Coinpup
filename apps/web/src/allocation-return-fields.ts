import type { components } from "./generated/openapi";
import { amountFromMinorUnits, parseAmount } from "./money";

type Source = components["schemas"]["AllocationReturnCapacityLine"];
export class ReturnFieldError extends Error {
    readonly code: "capacity" | "amount" | "rounding" | "source";
    constructor(code: "capacity" | "amount" | "rounding" | "source") {
        super(code);
        this.code = code;
    }
}

/** Preview the confirmed original-ratio rule; the server still validates fresh capacity. */
export function returnPaymentAmount(
    source: Source,
    documentAmount: string,
    paymentScale: number,
    documentScale: number,
): string {
    const originalP = parseAmount(source.payment_amount, paymentScale);
    const originalD = parseAmount(source.document_amount, documentScale);
    const returnedP = parseAmount(source.returned_payment_amount, paymentScale);
    const returnedD = parseAmount(source.returned_document_amount, documentScale);
    const remainingP = parseAmount(source.remaining_payment_amount, paymentScale);
    const remainingD = parseAmount(source.remaining_document_amount, documentScale);
    if (
        originalP <= 0n ||
        originalD <= 0n ||
        returnedP < 0n ||
        returnedD < 0n ||
        remainingP < 0n ||
        remainingD < 0n ||
        originalP !== returnedP + remainingP ||
        originalD !== returnedD + remainingD ||
        (remainingP === 0n) !== (remainingD === 0n)
    )
        throw new ReturnFieldError("source");
    const requestedD = parseAmount(documentAmount, documentScale);
    if (requestedD <= 0n) throw new ReturnFieldError("amount");
    if (requestedD > remainingD) throw new ReturnFieldError("capacity");
    const requestedP =
        requestedD === remainingD
            ? remainingP
            : (2n * requestedD * originalP + originalD) / (2n * originalD);
    if (
        requestedP <= 0n ||
        requestedP > remainingP ||
        (requestedP === remainingP) !== (requestedD === remainingD)
    )
        throw new ReturnFieldError("rounding");
    return amountFromMinorUnits(requestedP, paymentScale);
}
