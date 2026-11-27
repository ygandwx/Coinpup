import type { Asset } from "./ledger-api";

export const legacyNativeIds: ReadonlySet<string> = new Set(["BTC", "ETH", "XMR"]);

// Preserve the identity returned by the asset API, including reference case.
export function assetIdentity({
    code,
    kind,
    network,
    token_reference,
}: Pick<Asset, "code" | "kind" | "network" | "token_reference">): string | undefined {
    if (kind === "custodial")
        return network === null && token_reference === null ? `custodial:${code}` : undefined;
    if (kind === "fiat") return network === null && token_reference === null ? code : undefined;
    if (typeof network !== "string" || !network) return undefined;
    if (kind === "native" && token_reference === null)
        return legacyNativeIds.has(code) ? code : `native:${network}:${code}`;
    if (kind === "token" && typeof token_reference === "string" && token_reference)
        return `token:${network}:${token_reference}`;
    return undefined;
}
