import { Icon } from "./Icon";
import type { Entity } from "./ledger-api";
import "./icon-badges.css";

const countries = new Map([
    ["CN", "cn"],
    ["HK", "hk"],
    ["US", "us"],
    ["EE", "ee"],
]);
const states = new Set(["NM", "WY"]);
const companyTones = ["jade", "ochre", "iris", "blue", "rose", "slate"] as const;

export function EntityFlag({ entity, compact = false }: { entity: Entity; compact?: boolean }) {
    const isCompany = entity.kind === "company";
    const flag = isCompany && entity.country_code ? countries.get(entity.country_code) : undefined;
    const state =
        entity.country_code === "US" && entity.region_code && states.has(entity.region_code)
            ? entity.region_code
            : undefined;
    const tone = isCompany
        ? companyTones[parseInt(entity.id.slice(-6), 16) % companyTones.length]
        : "personal";
    return (
        <span
            aria-hidden="true"
            className={`entity-mark icon-badge${compact ? " entity-mark-compact" : ""}`}
            data-kind={entity.kind}
            data-tone={tone}
            data-country={flag ? entity.country_code : undefined}
        >
            {flag ? (
                <>
                    <img className="flag-image" src={`/assets/icons/flags/${flag}.svg`} alt="" />
                    {state && <span className="icon-corner">{state}</span>}
                </>
            ) : (
                <Icon name={isCompany ? "company" : "personal"} />
            )}
        </span>
    );
}
