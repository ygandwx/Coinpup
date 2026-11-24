import type { Session } from "./api";
import { getInvoiceProfile } from "./invoice-api";
import type { StoredFile } from "./files-api";
import type { InvoiceProfile } from "./invoice-issuance-receipt";
import { validReviewedInvoiceProfile } from "./invoice-web-intent";
import { freezeProfile, matchesProfile, saveProfile } from "./invoice-profile-intent";
import type { FrozenProfile, ProfileAction, ProfileBody } from "./invoice-profile-intent";
import { VersionedMasterController } from "./versioned-master-controller";

export class PendingInvoiceProfiles extends VersionedMasterController<
    FrozenProfile,
    InvoiceProfile
> {
    constructor(
        send: (s: Session, p: FrozenProfile) => Promise<unknown> = (s, p) =>
            saveProfile(s.csrf_token, p),
        read: (p: FrozenProfile) => Promise<unknown> = (p) => getInvoiceProfile(p.ledgerId, p.id),
    ) {
        super({
            send,
            read,
            create: (p) => p.action === "create",
            scoped: (v, p): v is InvoiceProfile =>
                validReviewedInvoiceProfile(v, p.ledgerId) && v.id === p.id,
            matches: matchesProfile,
            capture: (v) => Object.freeze({ ...v, numbering: Object.freeze({ ...v.numbering }) }),
        });
    }
    start(
        s: Session,
        ledger: string,
        id: string,
        action: ProfileAction,
        body: ProfileBody,
        previous: InvoiceProfile | null,
        logo: StoredFile | null = null,
    ) {
        return this.begin(s, () =>
            freezeProfile(String(s.user.id), ledger, id, action, body, previous, logo),
        );
    }
}
