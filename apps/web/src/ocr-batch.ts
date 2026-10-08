import { ApiError } from "./api";
import type { ReviewView } from "./ocr-api";
import { savedAction, savedDestination } from "./ocr-entry";
import type { ConfirmationInput } from "./pending-confirmations";

export function reviewedInput(ledger: string, file: string, review: ReviewView): ConfirmationInput {
    const action = savedAction(review.review.entry),
        confirmed = review.review.confirmed ?? [];
    if (
        review.status !== "draft" ||
        !action ||
        review.fields.some(
            (field) => field.requires_confirmation && !confirmed.includes(field.path),
        )
    )
        throw new ApiError("server", 422, "ocr_batch_not_ready");
    const target = savedDestination(review.review.entry) ?? { ledger, file };
    return {
        ledgerId: ledger,
        draftId: review.draft_id,
        body: {
            expected_version: review.version,
            target_ledger_id: target.ledger,
            target_file_id: target.file,
            confirmed: [...confirmed],
            duplicate_ack: false,
            entry: action,
        },
    };
}
