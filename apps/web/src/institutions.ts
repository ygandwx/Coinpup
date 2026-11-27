export type InstitutionRegion = "CN" | "HK" | "US" | "EE" | "EU" | "GB";

export type Institution = {
    id: string;
    name: { zh: string; en: string };
    shortName: string;
    color: string;
    swiftPrefix: string;
    region: InstitutionRegion;
};

function institution(
    id: string,
    region: InstitutionRegion,
    zh: string,
    en: string,
    shortName: string,
    color: string,
    swiftPrefix: string,
): Institution {
    return { id, region, name: { zh, en }, shortName, color, swiftPrefix };
}

// Prefill the eight-character institution and location code. A bank can
// have multiple codes; the account form leaves its SWIFT/BIC value editable.
export const INSTITUTIONS: readonly Institution[] = [
    // ICBC's participating banks list: https://singapore.icbc.com.cn/cn/page/721853568360677402.html
    institution(
        "icbc-cn",
        "CN",
        "中国工商银行",
        "Industrial and Commercial Bank of China",
        "ICBC",
        "#a52225",
        "ICBKCNBJ",
    ),
    institution(
        "abc-cn",
        "CN",
        "中国农业银行",
        "Agricultural Bank of China",
        "ABC",
        "#087260",
        "ABOCCNBJ",
    ),
    institution("boc-cn", "CN", "中国银行", "Bank of China", "BOC", "#a71929", "BKCHCNBJ"),
    institution(
        "ccb-cn",
        "CN",
        "中国建设银行",
        "China Construction Bank",
        "CCB",
        "#174c89",
        "PCBCCNBJ",
    ),
    institution(
        "bocom-cn",
        "CN",
        "交通银行",
        "Bank of Communications",
        "BOCOM",
        "#1e4581",
        "COMMCNSH",
    ),
    institution("cmb-cn", "CN", "招商银行", "China Merchants Bank", "CMB", "#ae1d2d", "CMBCCNBS"),
    institution("cib-cn", "CN", "兴业银行", "Industrial Bank", "CIB", "#17386c", "FJIBCNBA"),
    institution(
        "spdb-cn",
        "CN",
        "上海浦东发展银行",
        "Shanghai Pudong Development Bank",
        "SPDB",
        "#20597a",
        "SPDBCNSH",
    ),
    institution("pingan-cn", "CN", "平安银行", "Ping An Bank", "平安", "#974116", "SZDBCNBS"),
    institution("hsbc-cn", "CN", "汇丰银行（中国）", "HSBC China", "HSBC", "#b00f2a", "HSBCCNSH"),
    // https://www.hsbc.com.hk/help/
    institution("hsbc-hk", "HK", "香港汇丰银行", "HSBC Hong Kong", "HSBC", "#b00f2a", "HSBCHKHH"),
    // https://www.bochk.com/dam/document/003ecert.pdf
    institution(
        "boc-hk",
        "HK",
        "中国银行（香港）",
        "Bank of China (Hong Kong)",
        "BOCHK",
        "#a71929",
        "BKCHHKHH",
    ),
    // https://www.hangseng.com/business/your-business-essentials/transfer-inward-remittance-faq/
    institution("hangseng-hk", "HK", "恒生银行", "Hang Seng Bank", "HSB", "#006553", "HASEHKHH"),
    // https://www.sc.com/en/uploads/sites/66/content/docs/pvb-singapore-bank-account-details-for-inward-remittance.pdf
    institution(
        "standard-chartered-hk",
        "HK",
        "渣打银行（香港）",
        "Standard Chartered Hong Kong",
        "SC",
        "#196443",
        "SCBLHKHH",
    ),
    // https://www.chase.com/digital/wire-transfer/faqs
    institution("chase-us", "US", "大通银行", "Chase", "Chase", "#125493", "CHASUS33"),
    // https://info.bankofamerica.com/en/digital-banking/wire-transfers
    institution("bofa-us", "US", "美国银行", "Bank of America", "BofA", "#a51a36", "BOFAUS3N"),
    // https://www.wellsfargo.com/help/routing-number/
    institution("wells-fargo-us", "US", "富国银行", "Wells Fargo", "WF", "#a31b22", "WFBIUS6S"),
    // https://www.lhv.ee/en/payments/
    institution("lhv-ee", "EE", "LHV 银行", "LHV Bank", "LHV", "#332d29", "LHVBEE22"),
    // https://swedbank.ee/about
    institution(
        "swedbank-ee",
        "EE",
        "瑞典银行（爱沙尼亚）",
        "Swedbank Estonia",
        "SWED",
        "#93500f",
        "HABAEE2X",
    ),
    // https://www.seb.ee/eraklient/igapaevapangandus/maksed/korrespondentpangad
    institution(
        "seb-ee",
        "EE",
        "北欧斯安银行（爱沙尼亚）",
        "SEB Estonia",
        "SEB",
        "#407222",
        "EEUHEE2X",
    ),
    // https://luminor.ee/retail/incoming-cross-border-payment
    institution(
        "luminor-ee",
        "EE",
        "Luminor 银行（爱沙尼亚）",
        "Luminor Estonia",
        "LUM",
        "#682143",
        "RIKOEE22",
    ),
    // https://wise.com/us/swift-codes/TRWIBEBBXXX
    institution("wise-eu", "EU", "Wise 欧洲", "Wise Europe", "Wise", "#206132", "TRWIBEBB"),
    // https://www.revolut.com/de-DE/money-transfer/send-money-to-germany-from-marshall-islands/
    institution("revolut-eu", "EU", "Revolut 银行", "Revolut Bank", "Rev", "#2c325b", "REVOLT21"),
];

export function getInstitution(id?: string | null): Institution | undefined {
    return INSTITUTIONS.find((item) => item.id === id);
}

export function accountInstitution(details: Record<string, string>): Institution | undefined {
    const saved = details.institution;
    if (saved) return getInstitution(saved);
    const swift = details.swift_bic?.trim().toUpperCase();
    const name = details.bank_name?.trim().toLowerCase();
    if (swift) {
        const known = INSTITUTIONS.find((item) => swift.slice(0, 8) === item.swiftPrefix);
        if (known) return known;
    }
    const matches = INSTITUTIONS.filter(
        (item) =>
            item.name.en.toLowerCase() === name ||
            item.name.zh === name ||
            item.shortName.toLowerCase() === name,
    );
    return matches.length === 1 ? matches[0] : undefined;
}
