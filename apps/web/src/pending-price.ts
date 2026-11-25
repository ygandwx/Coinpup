import { ApiError } from "./api";
import type { Session } from "./api";
import { parseAmount } from "./money";
import { readManualPrices, saveManualPrice } from "./valuation-api";
import type { ManualPrice, ManualPriceInput } from "./valuation-api";
import { VersionedMasterController } from "./versioned-master-controller";
import type { MasterIdentity } from "./versioned-master-controller";

type Plan = MasterIdentity & { body: Readonly<ManualPriceInput> };

export class PendingPrice extends VersionedMasterController<Plan, ManualPrice> {
    constructor() {
        super({
            send: (session, plan) => saveManualPrice(session.csrf_token, plan.body),
            read: async ({ body: { asset_id, display_asset_id, price_date } }) =>
                (await readManualPrices({ asset_id, display_asset_id, price_date, limit: 1 }))
                    .items[0],
            create: () => false, // Both an occupied slot and stale edit use version_conflict.
            scoped: (value, { body }): value is ManualPrice => {
                if (typeof value !== "object" || value === null) return false;
                const record = value as Partial<ManualPrice>;
                return (
                    typeof record.id === "string" &&
                    typeof record.updated_at === "string" &&
                    typeof record.version === "number" &&
                    Number.isSafeInteger(record.version) &&
                    record.version > 0 &&
                    record.asset_id === body.asset_id &&
                    record.display_asset_id === body.display_asset_id &&
                    record.price_date === body.price_date
                );
            },
            matches: (value, { body }) => {
                try {
                    return (
                        value.source === body.source &&
                        (value.version === body.expected_version ||
                            value.version === body.expected_version + 1) &&
                        parseAmount(value.price, 18) === parseAmount(body.price, 18)
                    );
                } catch {
                    return false;
                }
            },
            capture: (value) => Object.freeze({ ...value }),
        });
    }
    start(session: Session, body: ManualPriceInput): Promise<void> {
        return this.begin(session, () => {
            if (parseAmount(body.price, 18) <= 0n)
                throw new ApiError("server", 422, "invalid_price");
            return {
                ownerId: String(session.user.id),
                ledgerId: "",
                id: JSON.stringify([body.asset_id, body.display_asset_id, body.price_date]),
                bodyJson: JSON.stringify(body),
                body: Object.freeze({ ...body }),
            };
        });
    }
}
