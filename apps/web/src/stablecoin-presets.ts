// Mainnet identities checked 2026-11-27. Preserve the issuer-listed address case.
// Tether networks and addresses: https://tether.to/en/supported-protocols/
// USDT precision from the issuer-linked contracts:
// https://docs.tronscan.org/en/api/tokens/token-trc20
// https://eth.blockscout.com/api/v2/tokens/0xdac17f958d2ee523a2206206994597c13d831ec7
// Circle networks, addresses and precision:
// https://developers.circle.com/stablecoins/usdc-contract-addresses
// https://github.com/circlefin/skills/blob/master/plugins/circle/skills/use-usdc/SKILL.md
export const stablecoinPresets = [
    {
        id: "usdt-tron",
        label: "USDT · TRON (TRC-20)",
        code: "USDT",
        network: "tron",
        reference: "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        scale: "6",
    },
    {
        id: "usdt-ethereum",
        label: "USDT · Ethereum (ERC-20)",
        code: "USDT",
        network: "ethereum",
        reference: "0xdac17f958d2ee523a2206206994597c13d831ec7",
        scale: "6",
    },
    {
        id: "usdc-ethereum",
        label: "USDC · Ethereum (ERC-20)",
        code: "USDC",
        network: "ethereum",
        reference: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        scale: "6",
    },
    {
        id: "usdc-solana",
        label: "USDC · Solana",
        code: "USDC",
        network: "solana",
        reference: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        scale: "6",
    },
] as const;
