import type { Account, AccountCreateBody, Asset, AssetPreset } from "./ledger-api";

export const OTHER_PLATFORM = "other";

type PlatformProfile = NonNullable<AccountCreateBody["crypto_platform"]>;
export type CoinUpdate = NonNullable<AccountCreateBody["crypto_assets"]>[number];
export type CoinDraft = Record<"network" | "address" | "memo" | "label", string>;
export type CryptoAccountDraft = {
    platform: string;
    customName: string;
    uid: string;
    email: string;
    note: string;
    coins: Record<string, CoinDraft>;
};

const optional = (value: string) => value.trim() || null;

export function coinDraft(value?: CoinUpdate["details"]): CoinDraft {
    return {
        network: value?.network ?? "",
        address: value?.address ?? "",
        memo: value?.memo ?? "",
        label: value?.label ?? "",
    };
}

export function cryptoAccountDraft(account?: Account): CryptoAccountDraft {
    const profile = account?.crypto_platform;
    return {
        platform: profile?.institution_id ?? (profile?.custom_name ? OTHER_PLATFORM : ""),
        customName: profile?.custom_name ?? "",
        uid: profile?.uid ?? "",
        email: profile?.email ?? "",
        note: profile?.note ?? "",
        coins: Object.fromEntries(
            (account?.crypto_assets ?? []).map(({ asset_id, details }) => [
                asset_id,
                coinDraft(details),
            ]),
        ),
    };
}

export function cryptoAccountUpdates(
    before: CryptoAccountDraft,
    next: CryptoAccountDraft,
    selected: readonly string[],
): { crypto_platform?: PlatformProfile; crypto_assets?: CoinUpdate[] } {
    const profile = (draft: CryptoAccountDraft): PlatformProfile => ({
        institution_id: draft.platform === OTHER_PLATFORM ? null : optional(draft.platform),
        custom_name: draft.platform === OTHER_PLATFORM ? optional(draft.customName) : null,
        uid: optional(draft.uid),
        email: optional(draft.email),
        note: optional(draft.note),
    });
    const current = profile(next);
    const updates: CoinUpdate[] = [];
    for (const asset_id of selected) {
        const draft = next.coins[asset_id] ?? coinDraft();
        if (JSON.stringify(draft) === JSON.stringify(before.coins[asset_id] ?? coinDraft()))
            continue;
        const details = {
            network: optional(draft.network),
            address: optional(draft.address),
            memo: optional(draft.memo),
            label: optional(draft.label),
        };
        updates.push({ asset_id, details: Object.values(details).some(Boolean) ? details : null });
    }
    return {
        ...(JSON.stringify(current) !== JSON.stringify(profile(before))
            ? { crypto_platform: current }
            : {}),
        ...(updates.length ? { crypto_assets: updates } : {}),
    };
}

type CatalogAsset = Pick<
    Asset,
    "asset_id" | "code" | "kind" | "network" | "token_reference" | "scale" | "enabled" | "verified"
>;
export type CryptoAssetChoice = CatalogAsset & { needsRegistration?: true };

const evmNetworks = new Set([
    "ethereum",
    "bsc",
    "polygon",
    "arbitrum",
    "optimism",
    "base",
    "avalanche",
]);

export function cryptoAssetChoices(
    assets: readonly CatalogAsset[],
    presets: readonly AssetPreset[],
): CryptoAssetChoice[] {
    const identity = (
        asset: Pick<CatalogAsset, "asset_id" | "kind" | "network" | "token_reference">,
    ) =>
        asset.kind === "token" &&
        asset.network &&
        evmNetworks.has(asset.network) &&
        /^0x[0-9a-f]{40}$/iu.test(asset.token_reference ?? "")
            ? asset.asset_id.toLowerCase()
            : asset.asset_id;
    const registered = new Set(assets.map(identity));
    return [
        ...assets,
        ...presets
            .filter((preset) => !registered.has(identity(preset)))
            .map((preset) => ({
                ...preset,
                enabled: true,
                verified: true,
                needsRegistration: true as const,
            })),
    ];
}
