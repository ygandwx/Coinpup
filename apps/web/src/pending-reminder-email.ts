import type { Session } from "./api";
import {
    listEmailDeliveries,
    readDelivery,
    saveEmailDecision,
    writeDelivery,
} from "./reminder-email-api";
import type {
    DeliverySettings,
    DeliveryView,
    EmailDecision,
    EmailDelivery,
} from "./reminder-email-api";
import { VersionedMasterController } from "./versioned-master-controller";
import type { MasterIdentity } from "./versioned-master-controller";

type Intent =
    | { kind: "settings"; body: Readonly<DeliverySettings> }
    | { kind: "decision"; delivery: string; body: Readonly<EmailDecision> };
type Plan = MasterIdentity & Intent;
type Record = DeliveryView | EmailDelivery;
const decisionStates = {
    retry: "retry_wait",
    verify_received: "verified_received",
    stop: "stopped",
};

async function readCurrent({ ledgerId, id, ...plan }: Plan) {
    if (plan.kind === "settings") return readDelivery(ledgerId, id);
    for (let offset = 0; offset <= 100000; offset += 50) {
        const rows = await listEmailDeliveries(ledgerId, id, offset, 50);
        const result = rows.find((row) => row.id === plan.delivery);
        if (result || rows.length < 50) return result;
    }
    return undefined;
}

function matchesSettings(record: DeliveryView, body: Readonly<DeliverySettings>) {
    const config = record.config;
    return (
        record.event_version === body.expected_event_version &&
        config !== null &&
        config.version >= Math.max(1, body.expected_config_version) &&
        config.enabled === body.enabled &&
        config.email_enabled === body.email_enabled &&
        config.email_recipient === body.email_recipient &&
        config.inbox_enabled === body.inbox_enabled &&
        config.lead_days === body.lead_days &&
        config.minute === body.minute &&
        config.timezone === body.timezone &&
        config.locale === body.locale
    );
}

export class PendingReminderEmail extends VersionedMasterController<Plan, Record> {
    constructor() {
        super({
            send: (session, plan) =>
                plan.kind === "settings"
                    ? writeDelivery(session.csrf_token, plan.ledgerId, plan.id, plan.body)
                    : saveEmailDecision(
                          session.csrf_token,
                          plan.ledgerId,
                          plan.id,
                          plan.delivery,
                          plan.body,
                      ),
            read: readCurrent,
            create: () => false,
            scoped: (value, plan): value is Record => {
                if (!value || typeof value !== "object") return false;
                const record = value as Record;
                return (
                    record.ledger_id === plan.ledgerId &&
                    record.event_id === plan.id &&
                    (plan.kind === "settings"
                        ? "config" in record &&
                          Number.isSafeInteger(record.event_version) &&
                          (record.config === null || Number.isSafeInteger(record.config?.version))
                        : "id" in record &&
                          record.id === plan.delivery &&
                          record.channel === "email" &&
                          Number.isSafeInteger(record.version))
                );
            },
            matches: (record, plan) =>
                plan.kind === "settings"
                    ? "config" in record && matchesSettings(record, plan.body)
                    : "id" in record &&
                      record.version === plan.body.expected_version + 1 &&
                      record.state === decisionStates[plan.body.action],
            changed: (record, plan) =>
                plan.kind === "settings"
                    ? "config" in record &&
                      (record.event_version > plan.body.expected_event_version ||
                          (record.config?.version ?? 0) > plan.body.expected_config_version)
                    : "id" in record && record.version > plan.body.expected_version,
            capture: (record) => Object.freeze(structuredClone(record)),
        });
    }

    start(session: Session, ledgerId: string, eventId: string, body: DeliverySettings) {
        return this.beginIntent(session, ledgerId, eventId, { kind: "settings", body });
    }

    decide(
        session: Session,
        ledgerId: string,
        eventId: string,
        delivery: string,
        body: EmailDecision,
    ) {
        return this.beginIntent(session, ledgerId, eventId, { kind: "decision", delivery, body });
    }

    private beginIntent(session: Session, ledgerId: string, eventId: string, intent: Intent) {
        return this.begin(
            session,
            () =>
                ({
                    ...intent,
                    body: Object.freeze({ ...intent.body }),
                    ownerId: String(session.user.id),
                    ledgerId,
                    id: eventId,
                    bodyJson: JSON.stringify(intent.body),
                }) as Plan,
        );
    }
}
