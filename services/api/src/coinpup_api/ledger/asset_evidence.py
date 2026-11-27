"""Mainnet asset denominations and contract identities checked with protocol sources."""

from datetime import date

VERIFIED_ON = date(2026, 11, 27)

# (code, mainnet key, decimals, sources)
NATIVE_EVIDENCE = (
    (
        "BTC",
        "bitcoin",
        8,
        ("https://github.com/bitcoin/bitcoin/blob/master/src/consensus/amount.h",),
    ),
    (
        "ETH",
        "ethereum",
        18,
        (
            "https://ethereum.org/en/developers/docs/intro-to-ether/",
            "https://github.com/ethereum/go-ethereum/blob/master/params/denomination.go",
        ),
    ),
    (
        "SOL",
        "solana",
        9,
        ("https://solana.com/developers/docs/core/accounts/account-structure",),
    ),
    (
        "BNB",
        "bsc",
        18,
        (
            "https://docs.bnbchain.org/bnb-greenfield/getting-started/general-faqs/",
            "https://github.com/bnb-chain/bsc/blob/master/params/denomination.go",
        ),
    ),
    (
        "TRX",
        "tron",
        6,
        ("https://developers.tron.network/docs/smart-contract-language",),
    ),
    (
        "XRP",
        "xrpl",
        6,
        ("https://xrpl.org/docs/references/protocol/data-types/currency-formats",),
    ),
    (
        "DOGE",
        "dogecoin",
        8,
        ("https://github.com/dogecoin/dogecoin/blob/master/src/amount.h",),
    ),
    (
        "LTC",
        "litecoin",
        8,
        ("https://github.com/litecoin-project/litecoin/blob/master/src/amount.h",),
    ),
    (
        "ADA",
        "cardano",
        6,
        ("https://docs.cardano.org/about-cardano/governance-overview",),
    ),
    (
        "TON",
        "ton",
        9,
        ("https://github.com/ton-org/ton-core/blob/main/src/utils/convert.ts",),
    ),
    (
        "AVAX",
        "avalanche",
        18,
        ("https://build.avax.network/explorer/mainnet/c-chain",),
    ),
    (
        "DOT",
        "polkadot",
        10,
        (
            "https://github.com/paritytech/polkadot-sdk/blob/master/polkadot/node/service/chain-specs/polkadot.json",
        ),
    ),
    (
        "POL",
        "polygon",
        18,
        (
            "https://docs.polygon.technology/pos/concepts/tokens/pol/",
            "https://github.com/0xPolygon/Polygon-Improvement-Proposals/blob/main/PIPs/PIP-17.md",
            "https://github.com/0xPolygon/bor/blob/develop/params/denomination.go",
        ),
    ),
    (
        "XMR",
        "monero",
        12,
        ("https://www.getmonero.org/resources/moneropedia/atomic-units.html",),
    ),
)

TETHER_PROTOCOLS = "https://tether.to/en/supported-protocols/"
TETHER_DECIMALS = (
    "https://github.com/tetherto/wdk-asset-registry/blob/"
    "a7b506cb51c1adb23af67273e8e0b6d26145ba3f/assets/common-tokens.json#L2-L18"
)
CIRCLE_CONTRACTS = "https://developers.circle.com/stablecoins/usdc-contract-addresses"
CIRCLE_DECIMALS = (
    "https://github.com/circlefin/skills/blob/master/plugins/circle/skills/use-usdc/SKILL.md"
)
FDUSD_CONTRACTS = "https://www.firstdigitallabs.com/fdusd"
FDUSD_DECIMALS = (
    "https://cdn.prod.website-files.com/675ab99bf1f7ea944d49a55b/"
    "6afe5884d7ce79e868f284e7_ISAE3000%20-%20Attestation%20Report%20on%20Reserves%20Account"
    "%20Oct%202026%29%20-%20Final.pdf"
)

# (code, mainnet key, contract reference, decimals, sources)
TOKEN_EVIDENCE = (
    (
        "USDT",
        "ethereum",
        "0xdac17f958d2ee523a2206206994597c13d831ec7",
        6,
        (
            TETHER_PROTOCOLS,
            "https://docs.wdk.tether.io/sdk/bridge-modules/bridge-usdt0-evm/guides/bridge-tokens/",
        ),
    ),
    (
        "USDT",
        "tron",
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        6,
        (
            TETHER_PROTOCOLS,
            "https://docs.wdk.tether.io/sdk/wallet-modules/wallet-tron/guides/transfer-tokens/",
        ),
    ),
    (
        "USDT",
        "solana",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        6,
        (
            TETHER_PROTOCOLS,
            "https://docs.wdk.tether.io/sdk/bridge-modules/bridge-usdt0-solana/guides/"
            "bridge-tokens/",
        ),
    ),
    (
        "USDT",
        "ton",
        "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        6,
        (
            TETHER_PROTOCOLS,
            "https://wdk.tether.io/blog/gasless-transactions-in-wdk-ton",
        ),
    ),
    (
        "USDT",
        "avalanche",
        "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7",
        6,
        (TETHER_PROTOCOLS, TETHER_DECIMALS),
    ),
    (
        "USDC",
        "ethereum",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        6,
        (CIRCLE_CONTRACTS, CIRCLE_DECIMALS),
    ),
    (
        "USDC",
        "solana",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        6,
        (CIRCLE_CONTRACTS, CIRCLE_DECIMALS),
    ),
    (
        "USDC",
        "polygon",
        "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        6,
        (CIRCLE_CONTRACTS, CIRCLE_DECIMALS),
    ),
    (
        "USDC",
        "arbitrum",
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        6,
        (CIRCLE_CONTRACTS, CIRCLE_DECIMALS),
    ),
    (
        "USDC",
        "optimism",
        "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        6,
        (CIRCLE_CONTRACTS, CIRCLE_DECIMALS),
    ),
    (
        "USDC",
        "base",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        6,
        (CIRCLE_CONTRACTS, CIRCLE_DECIMALS),
    ),
    (
        "USDC",
        "avalanche",
        "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        6,
        (CIRCLE_CONTRACTS, CIRCLE_DECIMALS),
    ),
    (
        "DAI",
        "ethereum",
        "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        18,
        (
            "https://github.com/sky-ecosystem/developerguides/blob/master/dai/dai-token/"
            "dai-token.md",
            "https://chainlog.sky.money/api/mainnet/active.json",
        ),
    ),
    (
        "DAI",
        "arbitrum",
        "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
        18,
        (
            "https://github.com/makerdao/arbitrum-dai-bridge/blob/"
            "ba5e98631307025a74fc03108492b20af57a0d3a/README.md#mainnet",
            "https://github.com/makerdao/arbitrum-dai-bridge/blob/"
            "ba5e98631307025a74fc03108492b20af57a0d3a/contracts/l2/dai.sol",
        ),
    ),
    (
        "DAI",
        "optimism",
        "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
        18,
        (
            "https://github.com/sky-ecosystem/optimism-dai-bridge/blob/"
            "bc3d63f5da2fd34ebb90369c90f2ea57e51cbca0/README.md#mainnet",
            "https://github.com/sky-ecosystem/optimism-dai-bridge/blob/"
            "bc3d63f5da2fd34ebb90369c90f2ea57e51cbca0/contracts/l2/dai.sol",
        ),
    ),
    (
        "FDUSD",
        "ethereum",
        "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409",
        18,
        (FDUSD_CONTRACTS, FDUSD_DECIMALS),
    ),
    (
        "FDUSD",
        "bsc",
        "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409",
        18,
        (FDUSD_CONTRACTS, FDUSD_DECIMALS),
    ),
    (
        "FDUSD",
        "solana",
        "9zNQRsGLjNKwCUU5Gq5LR8beUCPzQMVMqKAi3SSZh54u",
        6,
        (FDUSD_CONTRACTS, FDUSD_DECIMALS),
    ),
    (
        "FDUSD",
        "arbitrum",
        "0x93C9932E4afa59201F0B5E63f7d816516F1669fE",
        18,
        (FDUSD_CONTRACTS, FDUSD_DECIMALS),
    ),
    (
        "FDUSD",
        "ton",
        "EQD0Evpk4timFOHmy4Sv3l_KEUXlM-dN1_KhroTCfB2wkO89",
        6,
        (FDUSD_CONTRACTS, FDUSD_DECIMALS),
    ),
)
