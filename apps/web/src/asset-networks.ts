import type { Locale } from "./i18n";

const networkNames = new Map<string, readonly [string, string]>([
    ["bitcoin", ["比特币", "Bitcoin"]],
    ["ethereum", ["以太坊", "Ethereum"]],
    ["tron", ["波场", "TRON"]],
    ["solana", ["Solana", "Solana"]],
    ["bsc", ["BNB 智能链", "BNB Smart Chain"]],
    ["polygon", ["Polygon PoS", "Polygon PoS"]],
    ["arbitrum", ["Arbitrum One", "Arbitrum One"]],
    ["optimism", ["Optimism", "Optimism"]],
    ["base", ["Base", "Base"]],
    ["avalanche", ["Avalanche C 链", "Avalanche C-Chain"]],
    ["ton", ["TON", "TON"]],
    ["xrpl", ["XRP Ledger", "XRP Ledger"]],
    ["dogecoin", ["狗狗币", "Dogecoin"]],
    ["litecoin", ["莱特币", "Litecoin"]],
    ["cardano", ["Cardano", "Cardano"]],
    ["polkadot", ["波卡中继链", "Polkadot Relay Chain"]],
    ["monero", ["门罗币", "Monero"]],
]);

export function assetNetworkName(network: string, locale: Locale): string {
    return networkNames.get(network)?.[locale === "zh" ? 0 : 1] ?? network;
}
