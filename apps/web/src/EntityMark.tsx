import { Icon } from "./Icon";
import type { Entity } from "./ledger-api";
import "./entity-identity.css";

const companyTones = ["jade", "ochre", "iris", "blue", "rose", "slate"] as const;

export function EntityMark({ entity, compact = false }: { entity: Entity; compact?: boolean }) {
    // A company's mark stays recognizable when its display name or the UI language changes.
    const tone =
        entity.kind === "company"
            ? companyTones[parseInt(entity.id.slice(-6), 16) % companyTones.length]
            : "personal";
    return (
        <span
            aria-hidden="true"
            className={`entity-mark${compact ? " entity-mark-compact" : ""}`}
            data-kind={entity.kind}
            data-tone={tone}
        >
            <Icon name={entity.kind === "company" ? "company" : "personal"} />
        </span>
    );
}
