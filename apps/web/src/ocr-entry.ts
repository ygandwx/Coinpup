import type { Asset } from "./ledger-api";
import type { ConfirmationCreate, ReviewView } from "./ocr-api";
import type { PostingInput } from "./pending-command";

export type Entry = Exclude<ConfirmationCreate["entry"], { kind: "link" }>;
export type Action = ConfirmationCreate["entry"];
function record(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}
export function savedAction(value: unknown): Action | null {
    if (record(value) && value.kind === "link") {
        return typeof value.operation_id === "string" &&
            Number.isSafeInteger(value.expected_version) &&
            (value.expected_version as number) > 0
            ? (value as Action)
            : null;
    }
    return savedEntry(value);
}
/** Review storage is deliberately untyped; reject malformed snapshots before opening the form. */
export function savedEntry(value: unknown): Entry | null {
    if (!record(value) || !record(value.command)) return null;
    const body = value.command,
        kind = value.kind;
    if (
        typeof kind !== "string" ||
        !["opening", "income", "expense", "transfer", "exchange"].includes(kind)
    )
        return null;
    const keys = [
        "transaction_date",
        ...(kind === "transfer"
            ? ["source_account_id", "destination_account_id", "asset_id", "amount"]
            : kind === "exchange"
              ? [
                    "source_account_id",
                    "destination_account_id",
                    "source_asset_id",
                    "destination_asset_id",
                    "source_amount",
                    "destination_amount",
                ]
              : ["account_id", "asset_id", "amount"]),
    ];
    if (kind === "income" || kind === "expense") keys.push("recognition_date");
    if (keys.some((key) => typeof body[key] !== "string")) return null;
    if (
        [
            "account_id",
            "asset_id",
            "amount",
            "source_account_id",
            "destination_account_id",
            "source_asset_id",
            "destination_asset_id",
            "source_amount",
            "destination_amount",
            "recognition_date",
        ].some((key) => key in body && typeof body[key] !== "string")
    )
        return null;
    if (
        body.description !== undefined &&
        body.description !== null &&
        typeof body.description !== "string"
    )
        return null;
    if (kind === "income" || kind === "expense" || "splits" in body) {
        if (
            !Array.isArray(body.splits) ||
            body.splits.length > 100 ||
            body.splits.some(
                (s) =>
                    !record(s) || typeof s.category_id !== "string" || typeof s.amount !== "string",
            )
        )
            return null;
    }
    if (
        body.fees != null &&
        (!Array.isArray(body.fees) ||
            body.fees.length > 20 ||
            body.fees.some(
                (fee) =>
                    !record(fee) ||
                    ["account_id", "asset_id", "category_id", "amount"].some(
                        (key) => typeof fee[key] !== "string",
                    ),
            ))
    )
        return null;
    return value as Entry;
}
export function formInput(entry: Entry): PostingInput {
    return { kind: entry.kind, body: entry.command } as PostingInput;
}
export function preparedEntry(input: PostingInput): Entry {
    return { kind: input.kind, command: input.body } as Entry;
}
/** Only explicit source values: no account, direction, date parsing, rounding or currency guessing. */
export function initialEntry(
    kind: PostingInput["kind"],
    review: ReviewView,
    assets: Asset[],
    confirmed: string[],
    acceptReviewed: boolean,
): PostingInput {
    function value(roles: string[]) {
        const values = new Set(
            review.fields
                .filter((f) => roles.includes(f.path.split(".").at(-1) ?? ""))
                .map(
                    (f) =>
                        (f.source === "text" && !f.requires_confirmation
                            ? f.suggested_value
                            : null) ??
                        (acceptReviewed && confirmed.includes(f.path) ? f.candidate_value : null),
                )
                .filter((v): v is string => typeof v === "string"),
        );
        return values.size === 1 ? [...values][0] : "";
    }
    const currency = value(["currency"]),
        amount = value(["amount", "total"]),
        date = value(["date", "document_date"]);
    const matches = assets.filter(
        (a) => a.enabled && (a.asset_id === currency || a.code === currency),
    );
    const asset = currency && matches.length === 1 ? matches[0].asset_id : "";
    const common = {
        transaction_date: /^\d{4}-\d{2}-\d{2}$/u.test(date) ? date : "",
        description: "",
    };
    if (kind === "transfer")
        return {
            kind,
            body: {
                ...common,
                source_account_id: "",
                destination_account_id: "",
                asset_id: asset,
                amount,
            },
        };
    if (kind === "exchange")
        return {
            kind,
            body: {
                ...common,
                source_account_id: "",
                destination_account_id: "",
                source_asset_id: asset,
                source_amount: amount,
                destination_asset_id: "",
                destination_amount: "",
            },
        };
    if (kind === "opening")
        return { kind, body: { ...common, account_id: "", asset_id: asset, amount } };
    return {
        kind,
        body: {
            ...common,
            account_id: "",
            asset_id: asset,
            amount,
            recognition_date: "",
            splits: [{ category_id: "", amount }],
            fees: [],
        },
    };
}
