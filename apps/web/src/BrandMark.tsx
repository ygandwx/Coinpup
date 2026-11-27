import type { CSSProperties } from "react";

const platforms = ["wechat", "alipay", "paypal", "wise", "stripe"] as const;
type Platform = (typeof platforms)[number];

export function isPaymentPlatform(kind: string): kind is Platform {
    return platforms.some((name) => name === kind);
}

export function BrandMark({
    brand = "coinpup",
    size = 42,
}: {
    brand?: Platform | "coinpup";
    size?: number;
}) {
    return brand === "coinpup" ? (
        <img src="/coinpup.svg" alt="" aria-hidden="true" width={size} height={size} />
    ) : (
        <span
            className="payment-mark"
            aria-hidden="true"
            style={
                {
                    "--platform-image": `url("/assets/icons/payments/${brand}.svg")`,
                } as CSSProperties
            }
        />
    );
}
