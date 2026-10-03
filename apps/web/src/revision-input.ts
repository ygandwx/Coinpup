import type { FeeCreate, FinancialResponse, OperationState, Replacement } from "./ledger-api";
import type { PostingInput } from "./pending-command";

const copyFees = (fees: FeeCreate[] | undefined): FeeCreate[] =>
    (fees ?? []).map((fee) => ({
        account_id: fee.account_id,
        asset_id: fee.asset_id,
        amount: fee.amount,
        category_id: fee.category_id,
    }));

/** Copy only request fields. Persisted IDs, versions and journal metadata cannot leak into edits. */
function copyInput(input: PostingInput): PostingInput {
    const metadata = {
        transaction_date: input.body.transaction_date,
        description: input.body.description ?? "",
    };
    switch (input.kind) {
        case "opening":
            return {
                kind: "opening",
                body: {
                    ...metadata,
                    account_id: input.body.account_id,
                    asset_id: input.body.asset_id,
                    amount: input.body.amount,
                },
            };
        case "income":
        case "expense":
            return {
                kind: input.kind,
                body: {
                    ...metadata,
                    account_id: input.body.account_id,
                    asset_id: input.body.asset_id,
                    amount: input.body.amount,
                    recognition_date: input.body.recognition_date,
                    splits: input.body.splits.map((split) => ({
                        category_id: split.category_id,
                        amount: split.amount,
                    })),
                    fees: copyFees(input.body.fees),
                },
            };
        case "transfer":
            return {
                kind: "transfer",
                body: {
                    ...metadata,
                    source_account_id: input.body.source_account_id,
                    destination_account_id: input.body.destination_account_id,
                    asset_id: input.body.asset_id,
                    amount: input.body.amount,
                    fees: copyFees(input.body.fees),
                },
            };
        case "exchange":
            return {
                kind: "exchange",
                body: {
                    ...metadata,
                    source_account_id: input.body.source_account_id,
                    source_asset_id: input.body.source_asset_id,
                    source_amount: input.body.source_amount,
                    destination_account_id: input.body.destination_account_id,
                    destination_asset_id: input.body.destination_asset_id,
                    destination_amount: input.body.destination_amount,
                    fees: copyFees(input.body.fees),
                },
            };
    }
}

export function postingInputFromState(state: OperationState): PostingInput {
    const posting: FinancialResponse = state.latest_posting;
    switch (posting.kind) {
        case "opening":
            return copyInput({ kind: "opening", body: posting });
        case "income":
            return copyInput({ kind: "income", body: posting });
        case "expense":
            return copyInput({ kind: "expense", body: posting });
        case "transfer":
            return copyInput({ kind: "transfer", body: posting });
        case "exchange":
            return copyInput({ kind: "exchange", body: posting });
    }
}

export function replacementFromInput(input: PostingInput): Replacement {
    const clean = copyInput(input);
    switch (clean.kind) {
        case "opening":
            return { kind: "opening", ...clean.body };
        case "income":
            return { kind: "income", ...clean.body };
        case "expense":
            return { kind: "expense", ...clean.body };
        case "transfer":
            return { kind: "transfer", ...clean.body };
        case "exchange":
            return { kind: "exchange", ...clean.body };
    }
}
