import type { Locale } from "./i18n";

type Labels = Record<string, [string, string]>;
const label = (values: Labels, key: string, locale: Locale) =>
    values[key]?.[locale === "zh" ? 0 : 1] ?? key;
export const ruleLabel = (key: string, locale: Locale) =>
    label(
        {
            "cn.company.annual_report": ["中国大陆公司年度报告", "Mainland China annual report"],
            "us.wy.llc.annual_report": ["怀俄明州 LLC 年报", "Wyoming LLC annual report"],
            "hk.private.annual_return": [
                "香港私人公司周年申报",
                "Hong Kong private company annual return",
            ],
            "ee.private.annual_report": ["爱沙尼亚公司年度报告", "Estonian annual report"],
            "us.nm.llc.annual_report": ["新墨西哥州 LLC 年报", "New Mexico LLC annual report"],
            "certificate.expiry": ["证件记载的到期日", "Verified certificate expiry"],
        },
        key,
        locale,
    );
export const statusLabel = (key: string, locale: Locale) =>
    label(
        {
            calculated: ["已推算", "Calculated"],
            missing_parameters: ["待补充参数", "Missing inputs"],
            needs_verification: ["待确认／核验", "Needs verification"],
            not_applicable: ["不适用", "Not applicable"],
        },
        key,
        locale,
    );
export const reasonLabel = (key: string, locale: Locale) =>
    label(
        {
            scope_mismatch: [
                "与当前主体的地区或类型不匹配",
                "The entity jurisdiction or type does not match",
            ],
            applicability_declined: ["已确认本事项不适用", "This event was marked not applicable"],
            parameters_required: ["缺少必要参数", "Required inputs are missing"],
            applicability_unconfirmed: [
                "尚未确认本事项适用",
                "Applicability has not been confirmed",
            ],
            rule_not_verified: [
                "此版本规则尚未完成核验",
                "This rule version has not been verified",
            ],
            date_out_of_range: [
                "推算结果超出支持的日期范围",
                "The calculated date is outside the supported range",
            ],
            before_first_filing_year: [
                "早于首个适用申报年度",
                "Before the first applicable filing year",
            ],
        },
        key,
        locale,
    );
export const parameterLabel = (key: string, locale: Locale) =>
    label(
        {
            entity_kind: ["主体类型", "Entity kind"],
            country_code: ["地区", "Country / jurisdiction"],
            region_code: ["州", "State"],
            company_type: ["公司类型", "Company type"],
            registration_date: ["注册日期", "Registration date"],
            filing_year: ["申报年度", "Filing year"],
            period_end: ["实际财年结束日", "Actual financial year end"],
            expiry_date: ["证件到期日", "Certificate expiry"],
            anchor_date: ["基准日期", "Anchor date"],
            applicability_confirmed: ["适用性确认", "Applicability confirmation"],
        },
        key,
        locale,
    );
export const actionLabel = (key: string, locale: Locale) =>
    label(
        {
            create: ["创建", "Created"],
            edit: ["资料编辑", "Details edited"],
            recalculate: ["重算", "Recalculated"],
            set_manual: ["设置人工日期", "Manual date set"],
            clear_manual: ["清除人工日期", "Manual date cleared"],
            complete: ["完成", "Completed"],
            reopen: ["重开", "Reopened"],
            archive: ["归档", "Archived"],
            restore: ["恢复", "Restored"],
        },
        key,
        locale,
    );
