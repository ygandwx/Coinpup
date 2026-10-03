import type { Locale } from "./i18n";

export type LocalizedLabel = Record<Locale, string>;
export type CountryCode = "CN" | "HK" | "US" | "EE";
export type DetailField = { key: string; label: LocalizedLabel; help?: LocalizedLabel };

// Official terminology checked on 2026-10-03. These references describe profile
// fields only; they are not rules for calculating tax or filing deadlines.
export const REGION_SOURCES = {
  CN: [{ title: "国家税务总局 · 纳税人识别号", url: "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n1465977/n1466017/c1967505/content.html" }],
  HK: [
    { title: "Companies Registry · BRN / company number", url: "https://www.cr.gov.hk/en/faq/local-company/incorporation.htm?print=1" },
    { title: "Companies Registry · Alternative UBI", url: "https://www.cr.gov.hk/en/publications/docs/Speech_UBI_20231124_DRM_CSandM.pdf" },
  ],
  US: [
    { title: "IRS · EIN", url: "https://www.irs.gov/forms-pubs/about-form-ss-4" },
    { title: "New Mexico · Business registry", url: "https://enterprise.sos.nm.gov/search/business" },
    { title: "Wyoming · Filing ID", url: "https://wyobiz.wyo.gov/Business/Default.aspx" },
  ],
  EE: [
    { title: "RIK · Registry code / VAT number", url: "https://avaandmed.ariregister.rik.ee/en/open-data-api/company-requisite-information-service" },
    { title: "Estonia e-Residency · VAT registration", url: "https://learn.e-resident.gov.ee/hc/en-gb/articles/360000871738-VAT-registration" },
    { title: "RIK · Private limited company (OÜ)", url: "https://abiinfo.rik.ee/en/applications-and-dashboard/establishment-new-legal-person/establishment-private-limited-company" },
  ],
} as const;

type RegionProfile = {
  name: LocalizedLabel;
  companyType: "limited_liability" | "private_limited" | "llc";
  companyTypeLabel: LocalizedLabel;
  introduction: LocalizedLabel;
  fields: readonly DetailField[];
};

export const REGIONS: Record<CountryCode, RegionProfile> = {
  CN: {
    name: { zh: "中国大陆", en: "Mainland China" },
    companyType: "limited_liability",
    companyTypeLabel: { zh: "有限责任公司", en: "Limited liability company" },
    introduction: {
      zh: "已取得统一社会信用代码的企业，以该代码作为纳税人识别号。填写一次即可；其他或历史编号可在自定义资料中补充。",
      en: "A company with a Unified Social Credit Code uses that code as its taxpayer identification number. Enter it once; other or historical identifiers can be added as custom details.",
    },
    fields: [
      { key: "unified_social_credit_code", label: { zh: "统一社会信用代码", en: "Unified Social Credit Code" }, help: { zh: "营业执照上的 18 位代码，按原文填写。", en: "The 18-character code on the business licence. Copy it as issued." } },
      { key: "legal_representative", label: { zh: "法定代表人", en: "Legal representative" } },
    ],
  },
  HK: {
    name: { zh: "中国香港", en: "Hong Kong" },
    companyType: "private_limited",
    companyTypeLabel: { zh: "私人股份有限公司", en: "Private company limited by shares" },
    introduction: {
      zh: "自 2023 年 12 月 27 日起，BRN 作为公司的主要标识。原公司注册编号仅供保存历史资料；如公司注册处提供替代 UBI，可单独记录。",
      en: "Since 27 December 2023, BRN is the main company identifier. Keep a former company number for historical records, or record an alternative UBI if issued by the Companies Registry.",
    },
    fields: [
      { key: "business_registration_number", label: { zh: "商业登记号码（BRN）", en: "Business Registration Number (BRN)" }, help: { zh: "通常为商业登记证号码的前 8 位，不含后面的分段号码。", en: "Usually the first 8 digits of the Business Registration Certificate number, without the following segments." } },
      { key: "former_company_registration_number", label: { zh: "原公司注册编号", en: "Former Company Registration Number" } },
      { key: "alternative_ubi", label: { zh: "替代 UBI", en: "Alternative UBI" }, help: { zh: "仅在获分配时填写，保留字母前缀。", en: "Only if assigned; retain any alphabetic prefix." } },
      { key: "legal_name_other_language", label: { zh: "另一语言的法定名称", en: "Legal name in another language" }, help: { zh: "如同时登记了中英文名称，可在此填写另一个名称。", en: "If both Chinese and English names are registered, enter the other name here." } },
    ],
  },
  US: {
    name: { zh: "美国", en: "United States" },
    companyType: "llc",
    companyTypeLabel: { zh: "有限责任公司（LLC）", en: "Limited Liability Company (LLC)" },
    introduction: {
      zh: "州登记编号与 IRS 签发的 EIN 是不同标识。新墨西哥州称 Business ID，怀俄明州称 Filing ID；均可稍后补填。",
      en: "The state registration number and IRS-issued EIN are separate identifiers. New Mexico uses Business ID; Wyoming uses Filing ID. Both can be added later.",
    },
    fields: [
      { key: "state_registration_number", label: { zh: "州登记编号", en: "State registration number" }, help: { zh: "新墨西哥州：Business ID；怀俄明州：Filing ID。", en: "New Mexico: Business ID. Wyoming: Filing ID." } },
      { key: "ein", label: { zh: "雇主识别号码（EIN）", en: "EIN" }, help: { zh: "IRS 签发的 9 位号码，可保留连字符。", en: "The 9-digit number issued by the IRS; a hyphen may be retained." } },
      { key: "registered_agent_name", label: { zh: "注册代理人名称", en: "Registered agent name" } },
      { key: "registered_agent_address", label: { zh: "注册代理人地址", en: "Registered agent address" } },
    ],
  },
  EE: {
    name: { zh: "爱沙尼亚", en: "Estonia" },
    companyType: "private_limited",
    companyTypeLabel: { zh: "私人有限公司（OÜ）", en: "Private limited company (OÜ)" },
    introduction: {
      zh: "商业登记代码与 VAT 登记号码分别保存。成立公司不会自动取得 VAT 登记，未登记时可将 VAT 号码留空。",
      en: "Keep the registry code and VAT number separately. Company formation does not automatically register the company for VAT; leave the VAT number blank when not registered.",
    },
    fields: [
      { key: "registry_code", label: { zh: "商业登记代码", en: "Registry code" } },
      { key: "vat_number", label: { zh: "增值税登记号码", en: "VAT identification number" }, help: { zh: "如已登记，爱沙尼亚 VAT 号码为 EE 加 9 位数字。", en: "If registered, an Estonian VAT number is EE followed by 9 digits." } },
    ],
  },
};

export const COMPANY_CONTACT_FIELDS: readonly DetailField[] = [
  { key: "registered_address", label: { zh: "注册地址", en: "Registered address" } },
  { key: "contact_email", label: { zh: "联系邮箱", en: "Contact email" } },
  { key: "contact_phone", label: { zh: "联系电话", en: "Contact phone" } },
];

export const ACCOUNT_DETAIL_FIELDS: readonly DetailField[] = [
  { key: "account_holder", label: { zh: "账户持有人", en: "Account holder" } },
  { key: "account_number", label: { zh: "账号", en: "Account number" } },
  { key: "bank_name", label: { zh: "银行名称", en: "Bank name" } },
  { key: "branch_name", label: { zh: "开户行／支行", en: "Bank branch" } },
  { key: "iban", label: { zh: "IBAN", en: "IBAN" } },
  { key: "swift_bic", label: { zh: "SWIFT / BIC", en: "SWIFT / BIC" } },
  { key: "routing_number", label: { zh: "银行路由号码", en: "Routing number" } },
  { key: "wallet_address", label: { zh: "钱包地址", en: "Wallet address" } },
];

export type DetailRow = { id: string; key: string; value: string; custom?: boolean };

export function detailRows(details: Record<string, string>, builtInKeys: ReadonlySet<string>): DetailRow[] {
  return Object.entries(details).map(([key, value]) => ({ id: crypto.randomUUID(), key, value, custom: !builtInKeys.has(key) }));
}

export function replaceDetail(rows: DetailRow[], key: string, value: string): DetailRow[] {
  const existing = rows.find((row) => !row.custom && row.key === key);
  return existing
    ? rows.map((row) => row.id === existing.id ? { ...row, value } : row)
    : [...rows, { id: crypto.randomUUID(), key, value }];
}

// Preserve custom values, including intentional spaces. Empty built-in fields
// are omitted; an explicitly entered custom key can retain an empty value.
export function compileDetails(rows: DetailRow[], builtInKeys: ReadonlySet<string>): Record<string, string> {
  const result: [string, string][] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    if (!row.key && !row.value) continue;
    if (row.custom && builtInKeys.has(row.key)) throw new Error("reserved-detail");
    if (builtInKeys.has(row.key) && !row.value) continue;
    if (!row.key.trim() || row.key.length > 64 || /[\u0000-\u001f]/.test(row.key) || row.value.length > 2000 || row.value.includes("\0")) throw new Error("invalid-detail");
    if (seen.has(row.key)) throw new Error("duplicate-detail");
    seen.add(row.key);
    result.push([row.key, row.value]);
  }
  if (result.length > 32) throw new Error("too-many-details");
  return Object.fromEntries(result);
}
