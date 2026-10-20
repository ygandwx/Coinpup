import { useId } from "react";
import type { BusinessPaymentCreate } from "./business-settlement-api";
import type { PaymentReferences } from "./settlement-fields";
import type { Locale } from "./i18n";

type MoneyFields = { account_id: string; asset_id: string; amount: string };
export function PaymentMoneyFields({
    value,
    refs,
    locale,
    fee,
    identityLocked,
    amountLabel,
    onChange,
}: {
    value: MoneyFields;
    refs: PaymentReferences;
    locale: Locale;
    fee: boolean;
    identityLocked?: boolean;
    amountLabel?: string;
    onChange: (value: MoneyFields) => void;
}) {
    const prefix = useId();
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const assets = refs.assets.filter((row) => row.enabled);
    const accounts = refs.accounts.filter(
        (row) =>
            !row.archived &&
            row.asset_ids.some((id) => assets.some((asset) => asset.asset_id === id)),
    );
    const account = accounts.find((row) => row.id === value.account_id);
    const supported = assets.filter((row) => account?.asset_ids.includes(row.asset_id));
    return (
        <div className="form-grid">
            <div className="field">
                <label htmlFor={`${prefix}-account`}>
                    {fee ? t("手续费账户", "Fee account") : t("资金账户", "Money account")}
                </label>
                <select
                    id={`${prefix}-account`}
                    required
                    value={value.account_id}
                    onChange={(event) => onChange({ ...value, account_id: event.target.value })}
                >
                    <option value="">{t("请选择", "Choose")}</option>
                    {value.account_id && !accounts.some((row) => row.id === value.account_id) && (
                        <option value={value.account_id} disabled>
                            {t("当前账户不可用", "Current account unavailable")}
                        </option>
                    )}
                    {accounts.map((row) => (
                        <option key={row.id} value={row.id}>
                            {row.name}
                        </option>
                    ))}
                </select>
            </div>
            <div className="field">
                <label htmlFor={`${prefix}-asset`}>
                    {fee ? t("手续费资产", "Fee asset") : t("本金资产", "Principal asset")}
                </label>
                <select
                    id={`${prefix}-asset`}
                    required
                    disabled={identityLocked}
                    value={value.asset_id}
                    onChange={(event) => onChange({ ...value, asset_id: event.target.value })}
                >
                    <option value="">{t("请选择", "Choose")}</option>
                    {value.asset_id &&
                        !supported.some((row) => row.asset_id === value.asset_id) && (
                            <option value={value.asset_id} disabled>
                                {value.asset_id} · {t("不可用", "Unavailable")}
                            </option>
                        )}
                    {supported.map((row) => (
                        <option key={row.asset_id} value={row.asset_id}>
                            {row.asset_id}
                        </option>
                    ))}
                </select>
            </div>
            <div className="field">
                <label htmlFor={`${prefix}-amount`}>
                    {amountLabel ?? (fee ? t("手续费金额", "Fee amount") : t("本金", "Principal"))}
                </label>
                <input
                    id={`${prefix}-amount`}
                    type="text"
                    inputMode="decimal"
                    required
                    maxLength={40}
                    value={value.amount}
                    onChange={(event) => onChange({ ...value, amount: event.target.value })}
                />
                <small>
                    {t("最大小数位数", "Maximum decimal places")}:{" "}
                    {assets.find((row) => row.asset_id === value.asset_id)?.scale ?? "—"}
                </small>
            </div>
        </div>
    );
}

export function BusinessPaymentFields({
    value,
    refs,
    locale,
    locked,
    identityLocked,
    onChange,
}: {
    value: BusinessPaymentCreate;
    refs: PaymentReferences;
    locale: Locale;
    locked: boolean;
    identityLocked: boolean;
    onChange: (value: BusinessPaymentCreate) => void;
}) {
    const prefix = useId();
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const role = value.direction === "receipt" ? "customer" : "supplier";
    const parties = refs.parties.filter(
        (row) => !row.archived && row.name && [role, "both"].includes(row.role ?? ""),
    );
    const fees = value.fees ?? [];
    const categories = refs.categories.filter((row) => !row.archived && row.kind === "expense");
    return (
        <fieldset disabled={locked} className="draft-fields">
            <legend>{t("实际收付款", "Actual payment")}</legend>
            <p className="help-text">
                {t(
                    "本金先形成预收或预付；手续费是额外支出。不会重复确认单据收入或费用。",
                    "Principal first becomes a customer advance or supplier prepayment. Fees are additional expenses. Document income or expense is not posted again.",
                )}
            </p>
            {identityLocked && (
                <p role="status">
                    {t(
                        "已选择分摊明细。先明确移除全部分摊，才能更换方向、往来单位或本金资产。",
                        "Allocation lines are selected. Explicitly remove every line before changing direction, party or principal asset.",
                    )}
                </p>
            )}
            <div className="form-grid">
                <div className="field">
                    <label htmlFor={`${prefix}-direction`}>
                        {t("收付方向", "Payment direction")}
                    </label>
                    <select
                        id={`${prefix}-direction`}
                        value={value.direction}
                        disabled={identityLocked}
                        onChange={(event) =>
                            onChange({
                                ...value,
                                direction: event.target.value as "receipt" | "payout",
                            })
                        }
                    >
                        <option value="receipt">{t("客户收款", "Customer receipt")}</option>
                        <option value="payout">{t("供应商付款", "Supplier payout")}</option>
                    </select>
                </div>
                <div className="field">
                    <label htmlFor={`${prefix}-party`}>{t("往来单位", "Business party")}</label>
                    <select
                        id={`${prefix}-party`}
                        value={value.party_id}
                        required
                        disabled={identityLocked}
                        onChange={(event) => onChange({ ...value, party_id: event.target.value })}
                    >
                        <option value="">{t("请选择", "Choose")}</option>
                        {value.party_id && !parties.some((row) => row.id === value.party_id) && (
                            <option value={value.party_id} disabled>
                                {t("当前往来单位不可用", "Current party unavailable")}
                            </option>
                        )}
                        {parties.map((row) => (
                            <option key={row.id} value={row.id}>
                                {row.name}
                            </option>
                        ))}
                    </select>
                </div>
                <div className="field">
                    <label htmlFor={`${prefix}-date`}>{t("交易日", "Transaction date")}</label>
                    <input
                        id={`${prefix}-date`}
                        type="date"
                        required
                        value={value.transaction_date}
                        onChange={(event) =>
                            onChange({ ...value, transaction_date: event.target.value })
                        }
                    />
                </div>
            </div>
            <PaymentMoneyFields
                value={value}
                refs={refs}
                locale={locale}
                fee={false}
                identityLocked={identityLocked}
                onChange={(money) => onChange({ ...value, ...money })}
            />
            <div className="field">
                <label htmlFor={`${prefix}-description`}>{t("说明", "Description")}</label>
                <textarea
                    id={`${prefix}-description`}
                    maxLength={2000}
                    value={value.description ?? ""}
                    onChange={(event) => onChange({ ...value, description: event.target.value })}
                />
            </div>
            <h3>{t("独立手续费", "Separate fees")}</h3>
            {fees.map((fee, index) => (
                <fieldset key={index} className="draft-line">
                    <legend>
                        {t("手续费", "Fee")} {index + 1}
                    </legend>
                    <PaymentMoneyFields
                        value={fee}
                        refs={refs}
                        locale={locale}
                        fee
                        onChange={(money) =>
                            onChange({
                                ...value,
                                fees: fees.map((row, position) =>
                                    position === index ? { ...row, ...money } : row,
                                ),
                            })
                        }
                    />
                    <div className="field">
                        <label htmlFor={`${prefix}-fee-category-${index}`}>
                            {t("手续费分类", "Fee category")}
                        </label>
                        <select
                            id={`${prefix}-fee-category-${index}`}
                            required
                            value={fee.category_id}
                            onChange={(event) =>
                                onChange({
                                    ...value,
                                    fees: fees.map((row, position) =>
                                        position === index
                                            ? { ...row, category_id: event.target.value }
                                            : row,
                                    ),
                                })
                            }
                        >
                            <option value="">{t("请选择", "Choose")}</option>
                            {fee.category_id &&
                                !categories.some((row) => row.id === fee.category_id) && (
                                    <option value={fee.category_id} disabled>
                                        {t("当前分类不可用", "Current category unavailable")}
                                    </option>
                                )}
                            {categories.map((row) => (
                                <option key={row.id} value={row.id}>
                                    {row.name}
                                </option>
                            ))}
                        </select>
                    </div>
                    <button
                        type="button"
                        onClick={() =>
                            onChange({
                                ...value,
                                fees: fees.filter((_, position) => position !== index),
                            })
                        }
                    >
                        {t("移除此项手续费", "Remove this fee")}
                    </button>
                </fieldset>
            ))}
            <button
                type="button"
                disabled={fees.length >= 20}
                onClick={() =>
                    onChange({
                        ...value,
                        fees: [
                            ...fees,
                            { account_id: "", asset_id: "", amount: "", category_id: "" },
                        ],
                    })
                }
            >
                {t("添加手续费", "Add fee")}
            </button>
        </fieldset>
    );
}
