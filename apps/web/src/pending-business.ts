import { stripCommandText as masterName } from "./command-text";
import { ApiError } from "./api";
import type { Session } from "./api";
import { getMasterData, saveMasterData } from "./business-api";
import type { MasterBody, MasterKind, MasterRecord } from "./business-api";

import { VersionedMasterController } from "./versioned-master-controller";
import type { MasterStatus } from "./versioned-master-controller";
export type { MasterStatus };
export type FrozenMaster = Readonly<{
    ownerId: string;
    ledgerId: string;
    kind: MasterKind;
    id: string;
    method: "POST" | "PATCH";
    bodyJson: string;
}>;
export type MasterSnapshot = Readonly<{
    status: MasterStatus;
    plan: FrozenMaster | null;
    record: MasterRecord | null;
    error: ApiError | null;
}>;
type Transport = (session: Session, plan: FrozenMaster) => Promise<unknown>;
type Reader = (plan: FrozenMaster) => Promise<unknown>;
export { masterName };
function scoped(value: unknown, plan: FrozenMaster): value is MasterRecord {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const row = value as Record<string, unknown>;
    const text = (field: string) => row[field] === null || typeof row[field] === "string";
    return (
        row.id === plan.id &&
        row.ledger_id === plan.ledgerId &&
        Number.isInteger(row.version) &&
        (row.version as number) > 0 &&
        (row.version as number) <= 2147483647 &&
        typeof row.archived === "boolean" &&
        ["created_at", "updated_at"].every(
            (key) =>
                typeof row[key] === "string" && Number.isFinite(Date.parse(row[key] as string)),
        ) &&
        text("notes") &&
        (plan.kind === "projects"
            ? typeof row.name === "string"
            : text("name") &&
              (row.name === null
                  ? row.role === null
                  : ["customer", "supplier", "both"].includes(row.role as string)) &&
              ["legal_name", "email", "phone", "address", "tax_identifier"].every(text))
    );
}
function matches(value: MasterRecord, plan: FrozenMaster): boolean {
    const body = JSON.parse(plan.bodyJson) as Record<string, unknown>;
    const row = value as unknown as Record<string, unknown>;
    const expected =
        plan.method === "POST"
            ? {
                  notes: null,
                  archived: false,
                  ...(plan.kind === "parties"
                      ? {
                            legal_name: null,
                            email: null,
                            phone: null,
                            address: null,
                            tax_identifier: null,
                        }
                      : {}),
                  ...body,
              }
            : body;
    return (
        value.version === (plan.method === "POST" ? 1 : (body.expected_version as number) + 1) &&
        Object.entries(expected).every(
            ([key, item]) =>
                key === "expected_version" ||
                row[key] ===
                    (["name", "legal_name"].includes(key) && typeof item === "string"
                        ? masterName(item)
                        : item),
        )
    );
}

export class PendingMasterDataController extends VersionedMasterController<
    FrozenMaster,
    MasterRecord
> {
    constructor(
        io: Transport = (s, p) =>
            saveMasterData(
                s.csrf_token,
                p.ledgerId,
                p.kind,
                p.id,
                p.method,
                JSON.parse(p.bodyJson),
            ),
        read: Reader = (p) => getMasterData(p.ledgerId, p.kind, p.id),
    ) {
        super({
            send: io,
            read,
            create: (p) => p.method === "POST",
            scoped,
            matches,
            capture: (v) => Object.freeze({ ...v }),
        });
    }
    start(
        session: Session,
        ledgerId: string,
        kind: MasterKind,
        id: string,
        method: "POST" | "PATCH",
        body: MasterBody,
    ): Promise<void> {
        return this.begin(session, () => {
            const data = body as unknown as Record<string, unknown>;
            if (
                !ledgerId ||
                !id ||
                !["parties", "projects"].includes(kind) ||
                !["POST", "PATCH"].includes(method) ||
                (method === "POST"
                    ? data.id !== id
                    : !Number.isInteger(data.expected_version) ||
                      (data.expected_version as number) < 1 ||
                      (data.expected_version as number) > 2147483646)
            )
                throw new ApiError("server", null, "invalid_master_intent");
            return {
                ownerId: String(session.user.id),
                ledgerId,
                kind,
                id,
                method,
                bodyJson: JSON.stringify(body),
            };
        });
    }
}
