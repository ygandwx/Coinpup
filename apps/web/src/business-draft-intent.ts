import type { BusinessDraft, DraftAction } from "./business-draft-api";
import type { PricingInput } from "./business-pricing";
import { priceBusinessDocument } from "./business-pricing";
import { parseAmount } from "./money";
import { masterName } from "./pending-business";

export type FrozenDraft = Readonly<{
    ownerId: string;
    ledgerId: string;
    id: string;
    action: DraftAction;
    assetId: string;
    scale: number;
    bodyJson: string;
}>;
const object = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string";
const nullable = (v: unknown) => v === null || text(v);
const version = (v: unknown) =>
    typeof v === "number" && Number.isInteger(v) && v > 0 && v <= 2147483647;
const amounts = ["net_amount", "tax_amount", "total_amount"] as const;
function quantities(value: Record<string, unknown>): bigint[] {
    const values = amounts.map((key) => {
        if (!text(value[key])) throw new Error("invalid_amount");
        const units = parseAmount(value[key], 18);
        if (units < 0n) throw new Error("invalid_amount");
        return units;
    });
    if (values[0] + values[1] !== values[2]) throw new Error("invalid_total");
    return values;
}

/** A later conflicting version may use another asset; don't apply the old intent's precision. */
export function scopedDraft(value: unknown, plan: FrozenDraft): value is BusinessDraft {
    if (
        !object(value) ||
        value.id !== plan.id ||
        value.ledger_id !== plan.ledgerId ||
        !version(value.version) ||
        value.state !== "draft" ||
        typeof value.archived !== "boolean" ||
        (value.document_kind !== "invoice" && value.document_kind !== "bill") ||
        !text(value.asset_id) ||
        !text(value.party_id) ||
        !text(value.issue_date) ||
        !nullable(value.due_date) ||
        !nullable(value.notes) ||
        !object(value.issuer_snapshot) ||
        !object(value.party_snapshot) ||
        !Array.isArray(value.lines) ||
        value.lines.length > 200 ||
        value.line_count !== value.lines.length ||
        !text(value.created_at) ||
        !text(value.updated_at)
    )
        return false;
    try {
        const ids = new Set<string>();
        const sums = [0n, 0n, 0n];
        let position = 0;
        for (const line of value.lines) {
            if (
                !object(line) ||
                !text(line.id) ||
                ids.has(line.id) ||
                line.ledger_id !== plan.ledgerId ||
                line.document_id !== plan.id ||
                line.asset_id !== value.asset_id ||
                !version(line.version) ||
                typeof line.line_no !== "number" ||
                !version(line.line_no) ||
                line.line_no <= position ||
                line.archived !== false ||
                ![
                    "description",
                    "quantity",
                    "unit_price",
                    "discount_amount",
                    "tax_rate_percent",
                    "category_id",
                    "recognition_date",
                    "created_at",
                    "updated_at",
                ].every((key) => text(line[key])) ||
                line.category_kind !== (value.document_kind === "invoice" ? "income" : "expense") ||
                !nullable(line.project_id) ||
                !object(line.category_snapshot) ||
                (line.project_id === null
                    ? line.project_snapshot !== null
                    : !object(line.project_snapshot))
            )
                return false;
            ids.add(line.id);
            position = line.line_no;
            quantities(line).forEach((quantity, i) => {
                sums[i] += quantity;
            });
        }
        return quantities(value).every((quantity, i) => quantity === sums[i]);
    } catch {
        return false;
    }
}

export function matchesDraft(value: BusinessDraft, plan: FrozenDraft): boolean {
    try {
        const body = JSON.parse(plan.bodyJson) as Record<string, unknown>;
        if (
            value.version !==
                (plan.action === "create" ? 1 : (body.expected_version as number) + 1) ||
            value.asset_id !== plan.assetId
        )
            return false;
        if (plan.action === "archive") {
            if (value.archived !== body.archived) return false;
            const price = priceBusinessDocument(value.lines, plan.scale);
            return (
                amounts.every((key) => value[key] === price[key]) &&
                value.lines.every((line, i) =>
                    amounts.every((key) => line[key] === price.lines[i][key]),
                )
            );
        }
        if (value.archived) return false;
        const header = { due_date: null, notes: null, ...body } as Record<string, unknown>;
        if (
            !["document_kind", "party_id", "asset_id", "issue_date", "due_date", "notes"].every(
                (key) => value[key as keyof BusinessDraft] === header[key],
            )
        )
            return false;
        const inputs = body.lines === undefined ? [] : body.lines;
        if (
            !Array.isArray(inputs) ||
            inputs.length !== value.lines.length ||
            new Set(inputs.map((line) => (object(line) ? line.id : null))).size !== inputs.length
        )
            return false;
        const price = priceBusinessDocument(inputs as PricingInput[], plan.scale);
        const rows = new Map(value.lines.map((line) => [line.id, line]));
        return (
            amounts.every((key) => value[key] === price[key]) &&
            inputs.every((source, i) => {
                if (!object(source) || !text(source.id) || !text(source.description)) return false;
                const line = rows.get(source.id);
                if (
                    !line ||
                    (plan.action === "create" && (line.version !== 1 || line.line_no !== i + 1))
                )
                    return false;
                const expected = {
                    discount_amount: "0",
                    tax_rate_percent: "0",
                    project_id: null,
                    ...source,
                    description: masterName(source.description),
                };
                return (
                    [
                        "id",
                        "description",
                        "quantity",
                        "unit_price",
                        "discount_amount",
                        "tax_rate_percent",
                        "category_id",
                        "project_id",
                        "recognition_date",
                    ].every(
                        (key) =>
                            line[key as keyof typeof line] ===
                            expected[key as keyof typeof expected],
                    ) && amounts.every((key) => line[key] === price.lines[i][key])
                );
            })
        );
    } catch {
        return false;
    }
}
