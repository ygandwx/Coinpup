import type { AccountKind, Asset } from "./ledger-api";
import type { DetailField, LocalizedLabel } from "./regions";

const field = (key: string, zh: string, en: string): DetailField => ({
    key,
    label: { zh, en },
});

export const ACCOUNT_DETAILS = {
    institution: field("institution", "银行", "Bank"),
    bank_region: field("bank_region", "银行地区", "Bank region"),
    bank_name: field("bank_name", "银行名称", "Bank name"),
    account_holder: field("account_holder", "户名", "Account holder"),
    account_number: field("account_number", "账号", "Account number"),
    branch_name: field("branch_name", "开户行", "Bank branch"),
    swift_bic: field("swift_bic", "SWIFT / BIC", "SWIFT / BIC"),
    cnaps_code: field("cnaps_code", "联行号", "CNAPS code"),
    routing_number: field("routing_number", "路由号码", "Routing number"),
    iban: field("iban", "IBAN", "IBAN"),
    hk_bank_code: field("hk_bank_code", "银行编号", "Bank code"),
    real_name: field("real_name", "实名", "Legal name"),
    note: field("note", "备注", "Notes"),
    email: field("email", "邮箱", "Email"),
    payment_account_id: field("payment_account_id", "账户 ID", "Account ID"),
    issuer_name: field("issuer_name", "发卡行", "Card issuer"),
    card_last_four: field("card_last_four", "卡号后四位", "Last four card digits"),
    statement_day: field("statement_day", "账单日", "Statement day"),
    payment_due_day: field("payment_due_day", "还款日", "Payment due day"),
    wallet_network: field("wallet_network", "网络", "Network"),
    wallet_address: field("wallet_address", "钱包地址", "Wallet address"),
    wallet_label: field("wallet_label", "标签", "Label"),
    wallet_storage: field("wallet_storage", "钱包存放方式", "Wallet storage"),
    cash_location: field("cash_location", "存放位置", "Storage location"),
} as const;

export const ACCOUNT_DETAIL_KEYS = new Set(Object.keys(ACCOUNT_DETAILS));
export type AccountDetailKey = keyof typeof ACCOUNT_DETAILS;

export const BANK_REGIONS: Record<string, LocalizedLabel> = {
    CN: { zh: "中国内地", en: "Mainland China" },
    HK: { zh: "香港", en: "Hong Kong" },
    US: { zh: "美国", en: "United States" },
    EE: { zh: "爱沙尼亚", en: "Estonia" },
    EU: { zh: "其他欧洲地区", en: "Other European region" },
    GB: { zh: "英国", en: "United Kingdom" },
};

const BANK_REGION_KEYS: Partial<Record<string, AccountDetailKey>> = {
    CN: "cnaps_code",
    HK: "hk_bank_code",
    US: "routing_number",
    EE: "iban",
    EU: "iban",
    GB: "iban",
};

const TYPE_FIELDS: Record<AccountKind, AccountDetailKey[]> = {
    bank: [
        "institution",
        "bank_region",
        "bank_name",
        "account_holder",
        "account_number",
        "branch_name",
        "swift_bic",
    ],
    cash: ["cash_location", "note"],
    wechat: ["account_number", "real_name", "note"],
    alipay: ["account_number", "real_name", "note"],
    paypal: ["email", "payment_account_id"],
    wise: ["email", "payment_account_id"],
    stripe: ["email", "payment_account_id"],
    credit_card: ["issuer_name", "card_last_four", "statement_day", "payment_due_day"],
    crypto: ["wallet_network", "wallet_address", "wallet_label", "wallet_storage"],
};

export function accountFields(kind: string, region: string): AccountDetailKey[] {
    const regionalKey = BANK_REGION_KEYS[region];
    if (kind === "bank" && regionalKey) return [...TYPE_FIELDS.bank, regionalKey];
    return Object.hasOwn(TYPE_FIELDS, kind) ? TYPE_FIELDS[kind as AccountKind] : [];
}

export function isAccountAssetVisible(
    asset: Pick<Asset, "kind" | "asset_id">,
    kind: string,
    selected: readonly string[],
    showAll: boolean,
): boolean {
    return (
        showAll ||
        selected.includes(asset.asset_id) ||
        (asset.kind === "fiat") === (kind !== "crypto")
    );
}

export function defaultAssetIds(kind: AccountKind, assets: readonly Asset[]): string[] {
    return (kind === "wechat" || kind === "alipay") &&
        assets.some((asset) => asset.asset_id === "CNY" && asset.enabled)
        ? ["CNY"]
        : [];
}

export const WALLET_NETWORKS: Record<string, LocalizedLabel> = {
    bitcoin: { zh: "比特币", en: "Bitcoin" },
    ethereum: { zh: "以太坊", en: "Ethereum" },
    evm: { zh: "其他 EVM 网络", en: "Other EVM network" },
    tron: { zh: "波场 TRON", en: "TRON" },
    solana: { zh: "Solana", en: "Solana" },
    monero: { zh: "门罗币", en: "Monero" },
};

export const WALLET_STORAGE: Record<string, LocalizedLabel> = {
    cold: { zh: "冷钱包", en: "Cold wallet" },
    hot: { zh: "热钱包", en: "Hot wallet" },
};

// Basic address shape only. Checksums and network membership are not verified.
// Sources: ethereum.org/developers/docs/accounts/, developers.tron.network/docs/account,
// solana.com/docs/core/accounts/account-structure, and Bitcoin BIPs 173/350.
export function validWalletAddress(network: string, address: string): boolean {
    if (!address) return true;
    switch (network.trim().toLowerCase()) {
        case "ethereum":
        case "evm":
            return /^0x[\da-fA-F]{40}$/.test(address);
        case "tron":
            return /^(?:T[1-9A-HJ-NP-Za-km-z]{33}|41[\da-fA-F]{40})$/.test(address);
        case "solana":
            return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address);
        case "monero":
            // Mainnet prefixes and lengths: https://www.getmonero.org/library/MoneroAddressesCheatsheet20201206.pdf
            return /^(?:[48][1-9A-HJ-NP-Za-km-z]{94}|4[1-9A-HJ-NP-Za-km-z]{105})$/.test(address);
        case "bitcoin": {
            const bech32 = /^(?:bc|tb|bcrt)1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{11,87}$/;
            return (
                /^[123mn2][1-9A-HJ-NP-Za-km-z]{25,34}$/.test(address) ||
                ((address === address.toUpperCase() || address === address.toLowerCase()) &&
                    address.length <= 90 &&
                    bech32.test(address.toLowerCase()))
            );
        }
        default:
            return /^[a-zA-Z0-9_.:-]{8,128}$/.test(address);
    }
}

export function validCardLastFour(value: string): boolean {
    return value === "" || /^[0-9]{4}$/.test(value);
}

export function validCardDay(value: string): boolean {
    return value === "" || /^(?:[1-9]|[12][0-9]|3[01])$/.test(value);
}
