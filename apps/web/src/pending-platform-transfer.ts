import { ApiError } from "./api";
import type { Session } from "./api";
import { stripCommandText } from "./command-text";
import type { Asset, FeeCreate } from "./ledger-api";
import { parseAmount } from "./money";
import { cancelPlatformTransfer, createPlatformTransfer } from "./platform-transfers-api";
import type {
    PlatformTransferCancel,
    PlatformTransferCancellation,
    PlatformTransferCreate,
    PlatformTransferDetail,
    PlatformTransferReceipt,
} from "./platform-transfers-api";
import { VersionedMasterController } from "./versioned-master-controller";
import type { MasterIdentity } from "./versioned-master-controller";

type Receipt = PlatformTransferReceipt | PlatformTransferCancellation;
type FrozenTransfer = MasterIdentity &
    Readonly<{ key: string; destinationLedger: string }> &
    (
        | Readonly<{
              kind: "create";
              sourceScale: number;
              destinationScale: number;
              feeScales: readonly number[];
          }>
        | Readonly<{ kind: "cancel" }>
    );
type Send = (session: Session, plan: FrozenTransfer) => Promise<unknown>;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

function record(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function identifier(value: unknown): value is string {
    return typeof value === "string" && uuid.test(value);
}

function completeLegs(value: unknown, plan: FrozenTransfer): boolean {
    const ledgers = new Set([plan.ledgerId, plan.destinationLedger]);
    if (!Array.isArray(value) || value.length !== ledgers.size) return false;
    const operations = new Set<string>();
    const journals = new Set<string>();
    for (const leg of value) {
        if (
            !record(leg) ||
            typeof leg.ledger_id !== "string" ||
            !ledgers.delete(leg.ledger_id) ||
            !identifier(leg.operation_id) ||
            !identifier(leg.journal_id) ||
            operations.has(leg.operation_id) ||
            journals.has(leg.journal_id)
        )
            return false;
        operations.add(leg.operation_id);
        journals.add(leg.journal_id);
    }
    return true;
}

function sameAmount(received: unknown, expected: string, scale: number): boolean {
    return (
        typeof received === "string" &&
        parseAmount(received, scale) === parseAmount(expected, scale)
    );
}

function sameFee(received: unknown, expected: FeeCreate, scale: number): boolean {
    return (
        record(received) &&
        received.account_id === expected.account_id &&
        received.asset_id === expected.asset_id &&
        received.category_id === expected.category_id &&
        sameAmount(received.amount, expected.amount, scale)
    );
}

function completeReceipt(value: unknown, plan: FrozenTransfer): value is Receipt {
    if (!record(value) || !identifier(value.id) || value.source_ledger_id !== plan.ledgerId)
        return false;
    if (plan.kind === "cancel") {
        const body: PlatformTransferCancel = JSON.parse(plan.bodyJson);
        return (
            value.id === plan.id &&
            value.status === "cancelled" &&
            value.version === body.expected_version + 1 &&
            value.reason === stripCommandText(body.reason) &&
            completeLegs(value.reversals, plan)
        );
    }
    const body: PlatformTransferCreate = JSON.parse(plan.bodyJson);
    const fees = body.fees ?? [];
    const receivedFees = value.fees;
    try {
        return (
            value.id === plan.id &&
            value.status === "active" &&
            value.version === 1 &&
            value.kind === body.kind &&
            value.source_account_id === body.source_account_id &&
            value.source_asset_id === body.source_asset_id &&
            value.destination_ledger_id === body.destination_ledger_id &&
            value.destination_account_id === body.destination_account_id &&
            value.destination_asset_id === body.destination_asset_id &&
            value.transaction_date === body.transaction_date &&
            value.description === stripCommandText(body.description ?? "") &&
            sameAmount(value.source_amount, body.amount, plan.sourceScale) &&
            sameAmount(value.destination_amount, body.amount, plan.destinationScale) &&
            Array.isArray(receivedFees) &&
            receivedFees.length === fees.length &&
            fees.every((fee, index) => sameFee(receivedFees[index], fee, plan.feeScales[index]!)) &&
            completeLegs(value.operations, plan)
        );
    } catch {
        return false;
    }
}

export class PendingPlatformTransfer extends VersionedMasterController<FrozenTransfer, Receipt> {
    constructor(
        send: Send = (session, plan) =>
            plan.kind === "create"
                ? createPlatformTransfer(
                      session.csrf_token,
                      plan.ledgerId,
                      JSON.parse(plan.bodyJson),
                      plan.key,
                  )
                : cancelPlatformTransfer(
                      session.csrf_token,
                      plan.ledgerId,
                      plan.id,
                      JSON.parse(plan.bodyJson),
                      plan.key,
                  ),
    ) {
        super({
            send,
            scoped: completeReceipt,
            matches: () => true,
            capture: (value) => {
                const receipt = structuredClone(value);
                if ("reversals" in receipt) {
                    receipt.reversals.forEach(Object.freeze);
                    Object.freeze(receipt.reversals);
                } else {
                    receipt.fees.forEach(Object.freeze);
                    Object.freeze(receipt.fees);
                    receipt.operations.forEach(Object.freeze);
                    Object.freeze(receipt.operations);
                }
                return Object.freeze(receipt);
            },
        });
    }

    create(
        session: Session,
        ledgerId: string,
        body: PlatformTransferCreate,
        source: Asset,
        destination: Asset,
        assets: readonly Asset[],
    ) {
        return this.begin(session, () => {
            if (
                parseAmount(body.amount, source.scale) <= 0n ||
                parseAmount(body.amount, destination.scale) <= 0n
            )
                throw new ApiError("server", 422, "amount_positive");
            const feeScales: number[] = [];
            for (const fee of body.fees ?? []) {
                const scale = assets.find((asset) => asset.asset_id === fee.asset_id)?.scale;
                if (scale === undefined || parseAmount(fee.amount, scale) <= 0n)
                    throw new ApiError("server", 422, "amount_positive");
                feeScales.push(scale);
            }
            const id = crypto.randomUUID();
            return {
                ownerId: String(session.user.id),
                ledgerId,
                destinationLedger: body.destination_ledger_id,
                id,
                key: crypto.randomUUID(),
                kind: "create",
                sourceScale: source.scale,
                destinationScale: destination.scale,
                feeScales: Object.freeze(feeScales),
                bodyJson: JSON.stringify({ ...body, id }),
            };
        });
    }

    cancel(session: Session, row: PlatformTransferDetail, reason: string) {
        return this.begin(session, () => ({
            ownerId: String(session.user.id),
            ledgerId: row.source_ledger_id,
            destinationLedger: row.destination_ledger_id,
            id: row.id,
            key: crypto.randomUUID(),
            kind: "cancel",
            bodyJson: JSON.stringify({
                expected_version: row.version,
                reason,
            } satisfies PlatformTransferCancel),
        }));
    }
}
