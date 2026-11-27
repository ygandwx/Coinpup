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
const crypto = new Set(["BTC", "ETH", "XMR", "USDT", "USDC"]);
const networkIcons = new Map([
    ["ethereum", "eth"],
    ["erc20", "eth"],
    ["tron", "trx"],
    ["trc20", "trx"],
    ["solana", "sol"],
]);

export function AssetIcon({
    asset,
    compact = false,
}: {
    asset: Pick<Asset, "code" | "kind" | "network">;
    compact?: boolean;
}) {
    const { code, kind, network } = asset;
    const currency = kind === "fiat" ? fiat.get(code) : undefined;
    const chain = network ? networkIcons.get(network) : undefined;

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
                    <img
                        className="coin-image"
                        src={`/assets/icons/crypto/${crypto.has(code) ? code.toLowerCase() : "generic"}.svg`}
                        alt=""
                    />
                    {kind === "token" && network && (
                        <span className="icon-corner" data-network={network}>
                            {chain ? (
                                <img src={`/assets/icons/crypto/${chain}.svg`} alt="" />
                            ) : (
                                network.slice(0, 3).toUpperCase()
                            )}
                        </span>
                    )}
                </>
            )}
        </span>
    );
}
