import type { Session } from "./api";
import { readDelivery, writeDelivery } from "./reminder-email-api";
import type { DeliverySettings, DeliveryView } from "./reminder-email-api";
import { VersionedMasterController } from "./versioned-master-controller";
import type { MasterIdentity } from "./versioned-master-controller";

type Plan = MasterIdentity & { body: Readonly<DeliverySettings> };

export class PendingReminderEmail extends VersionedMasterController<Plan, DeliveryView> {
    constructor() {
        super({
            send: (session, plan) =>
                writeDelivery(session.csrf_token, plan.ledgerId, plan.id, plan.body),
            read: (plan) => readDelivery(plan.ledgerId, plan.id),
            create: () => false, // A changed event or an occupied configuration uses version_conflict.
            scoped: (value, plan): value is DeliveryView => {
                if (!value || typeof value !== "object") return false;
                const record = value as DeliveryView;
                return (
                    record.ledger_id === plan.ledgerId &&
                    record.event_id === plan.id &&
                    Number.isSafeInteger(record.event_version) &&
                    (record.config === null || Number.isSafeInteger(record.config?.version))
                );
            },
            matches: (record, { body }) => {
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
            },
            changed: (record, { body }) =>
                record.event_version > body.expected_event_version ||
                (record.config?.version ?? 0) > body.expected_config_version,
            capture: (record) => Object.freeze(structuredClone(record)),
        });
    }
    start(session: Session, ledgerId: string, eventId: string, body: DeliverySettings) {
        return this.begin(session, () => ({
            body: Object.freeze({ ...body }),
            ownerId: String(session.user.id),
            ledgerId,
            id: eventId,
            bodyJson: JSON.stringify(body),
        }));
    }
}
