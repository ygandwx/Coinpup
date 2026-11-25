import { ApiError } from "./api";
import type { Session } from "./api";
import { recordRepayment, registerAdvance } from "./interledger-api";
import type {
    InterledgerAdvance,
    InterledgerAdvanceCreate,
    InterledgerRepayment,
    InterledgerRepaymentCreate,
} from "./interledger-api";
import { parseAmount } from "./money";
import { VersionedMasterController } from "./versioned-master-controller";
import type { MasterIdentity } from "./versioned-master-controller";

type Receipt = InterledgerAdvance | InterledgerRepayment;
type Intent =
    | { kind: "advance"; body: InterledgerAdvanceCreate }
    | { kind: "repayment"; advanceId: string; body: InterledgerRepaymentCreate };
type FrozenInterledger = MasterIdentity &
    Readonly<{
        kind: Intent["kind"];
        key: string;
        scale: number;
    }>;
type Send = (session: Session, plan: FrozenInterledger) => Promise<unknown>;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

function completeReceipt(value: unknown, plan: FrozenInterledger): value is Receipt {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
    const reply: Record<string, unknown> = value as Record<string, unknown>;
    const supplied = JSON.parse(plan.bodyJson);
    if (
        ![reply.id, reply.payer_operation_id, reply.beneficiary_operation_id].every(
            (id) => typeof id === "string" && uuid.test(id),
        ) ||
        reply.payer_operation_id === reply.beneficiary_operation_id ||
        typeof reply.amount !== "string" ||
        reply.transaction_date !== supplied.transaction_date ||
        reply.description !== (supplied.description ?? "")
    )
        return false;
    try {
        if (parseAmount(reply.amount, plan.scale) !== parseAmount(supplied.amount, plan.scale))
            return false;
        if (plan.kind === "repayment")
            return (
                reply.advance_id === plan.id &&
                reply.beneficiary_payment_account_id === supplied.beneficiary_payment_account_id &&
                reply.payer_receipt_account_id === supplied.payer_receipt_account_id
            );
        return (
            reply.payer_ledger_id === supplied.payer_ledger_id &&
            reply.beneficiary_ledger_id === supplied.beneficiary_ledger_id &&
            reply.payer_account_id === supplied.payer_account_id &&
            reply.expense_category_id === supplied.expense_category_id &&
            reply.asset_id === supplied.asset_id &&
            reply.outstanding_amount === reply.amount
        );
    } catch {
        return false;
    }
}

export class PendingInterledger extends VersionedMasterController<FrozenInterledger, Receipt> {
    constructor(
        send: Send = (s, p) => {
            const body = JSON.parse(p.bodyJson);
            return p.kind === "advance"
                ? registerAdvance(s.csrf_token, body, p.key)
                : recordRepayment(s.csrf_token, p.id, body, p.key);
        },
    ) {
        super({
            send,
            scoped: completeReceipt,
            matches: () => true,
            capture: (receipt) => Object.freeze({ ...receipt }),
        });
    }

    start(session: Session, ledgerId: string, scale: number, intent: Intent) {
        return this.begin(session, () => {
            if (parseAmount(intent.body.amount, scale) <= 0n)
                throw new ApiError("server", 422, "amount_positive");
            const key = crypto.randomUUID();
            return {
                ownerId: String(session.user.id),
                ledgerId,
                id: intent.kind === "advance" ? key : intent.advanceId,
                key,
                kind: intent.kind,
                scale,
                bodyJson: JSON.stringify(intent.body),
            };
        });
    }
}
