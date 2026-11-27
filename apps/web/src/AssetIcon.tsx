import type { Asset } from "./ledger-api";
import "./icon-badges.css";

const fiat = new Map<string, readonly [string, string]>([
    ["USD", ["us", "$"]],
    ["CNY", ["cn", "¥"]],
    ["HKD", ["hk", "$"]],
    ["EUR", ["eu", "€"]],
    ["GBP", ["gb", "£"]],
    ["JPY", ["jp", "¥"]],
    ["AUD", ["au", "$"]],
    ["CAD", ["ca", "$"]],
    ["CHF", ["ch", "Fr"]],
    ["SGD", ["sg", "$"]],
    ["NZD", ["nz", "$"]],
    ["KRW", ["kr", "₩"]],
    ["INR", ["in", "₹"]],
    ["TWD", ["tw", "$"]],
    ["MOP", ["mo", "P"]],
    ["SEK", ["se", "kr"]],
    ["NOK", ["no", "kr"]],
    ["DKK", ["dk", "kr"]],
    ["PLN", ["pl", "zł"]],
    ["MXN", ["mx", "$"]],
    ["BRL", ["br", "R$"]],
    ["AED", ["ae", "د.إ"]],
    ["SAR", ["sa", "ر.س"]],
    ["ZAR", ["za", "R"]],
]);
const crypto = new Set([
    "ADA",
    "AVAX",
    "BNB",
    "BTC",
    "DAI",
    "DOGE",
    "DOT",
    "ETH",
    "LTC",
    "SOL",
    "TRX",
    "USDC",
    "USDT",
    "XMR",
    "XRP",
]);
const textCoins = new Set(["FDUSD", "POL", "TON"]);
const networkIcons = new Map([
    ["bitcoin", "btc"],
    ["ethereum", "eth"],
    ["erc20", "eth"],
    ["tron", "trx"],
    ["trc20", "trx"],
    ["solana", "sol"],
    ["bsc", "bnb"],
    ["polygon", "matic"],
    ["avalanche", "avax"],
]);
const networkLabels = new Map([
    ["arbitrum", "ARB"],
    ["optimism", "OP"],
    ["base", "BASE"],
    ["ton", "TON"],
]);

export function AssetIcon({
    asset,
    compact = false,
    networkBadge,
}: {
    asset: Pick<Asset, "code" | "kind" | "network" | "verified">;
    compact?: boolean;
    networkBadge?: string | null;
}) {
    const { code, kind, network, verified } = asset;
    const badge = networkBadge ?? (kind === "token" ? network : null);
    const currency = kind === "fiat" ? fiat.get(code) : undefined;
    const chain = badge ? networkIcons.get(badge) : undefined;
    const brandedCoin = kind === "native" || kind === "custodial" || (kind === "token" && verified);

    return (
        <span
            className={`asset-mark icon-badge${compact ? " compact" : ""}`}
            aria-hidden="true"
            data-asset-code={code}
        >
            {kind === "fiat" ? (
                currency ? (
                    <>
                        <img
                            className="flag-image"
                            src={`/assets/icons/flags/${currency[0]}.svg`}
                            alt=""
                        />
                        <span className="icon-corner">{currency[1]}</span>
                    </>
                ) : (
                    <span className="asset-fallback-text">{code}</span>
                )
            ) : (
                <>
                    {brandedCoin && textCoins.has(code) ? (
                        <span className="asset-fallback-text">{code}</span>
                    ) : (
                        <img
                            className="coin-image"
                            src={`/assets/icons/crypto/${brandedCoin && crypto.has(code) ? code.toLowerCase() : "generic"}.svg`}
                            alt=""
                        />
                    )}
                    {badge && (
                        <span className="icon-corner" data-network={badge}>
                            {chain ? (
                                <img src={`/assets/icons/crypto/${chain}.svg`} alt="" />
                            ) : (
                                (networkLabels.get(badge) ?? badge.slice(0, 3).toUpperCase())
                            )}
                        </span>
                    )}
                </>
            )}
        </span>
    );
}
