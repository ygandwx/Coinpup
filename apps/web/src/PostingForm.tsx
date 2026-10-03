import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { Account, Asset, Category, FeeCreate } from "./ledger-api";
import type { PostingInput } from "./pending-command";
import { AmountError, parseAmount } from "./money";
import "./posting-form.css";

type Kind = PostingInput["kind"];
type Option = { value: string; label: string };
type SplitDraft = { id: string; categoryId: string; amount: string | null };
type FeeDraft = { id: string; accountId: string; assetId: string; amount: string; categoryId: string };
type Problem = "reference" | "date" | "description" | "positive" | "nonzero" | "format" | "precision" | "range" | "splits" | "duplicate" | "total" | "transfer" | "exchange" | "fees";
type Issue = { code: Problem; asset?: string; scale?: number };

const copy = {
  zh: {
    title: "记账表单", kind: "记账类型", account: "账户", asset: "资产", amount: "金额", sourceAccount: "转出账户", destinationAccount: "转入账户",
    sourceAsset: "转出资产", destinationAsset: "转入资产", sourceAmount: "转出金额", destinationAmount: "转入金额", transactionDate: "交易日期", recognitionDate: "确认日期", description: "备注",
    choose: "请选择", splitTitle: "分类拆分", split: "拆分", category: "分类", splitAmount: "拆分金额", addSplit: "添加拆分", removeSplit: "移除拆分", feeTitle: "手续费", fee: "手续费",
    feeAccount: "手续费账户", feeAsset: "手续费资产", feeAmount: "手续费金额", feeCategory: "手续费分类", addFee: "添加手续费", removeFee: "移除手续费", save: "保存", saving: "保存中…", cancel: "取消", sign: "切换正负",
    kinds: { opening: "期初余额", income: "收入", expense: "支出", transfer: "转账／信用卡还款", exchange: "换汇／资产兑换" },
    openingHelp: "期初余额可以为负数，不能为零。每个账户的每种资产只能设置一次期初。", splitHelp: "拆分金额合计须与本金一致，分类不可重复。只有一项且尚未编辑时，拆分金额随本金填写。",
    transferHelp: "在同一账本的不同账户之间转移同一种资产，信用卡还款也使用此方式。", exchangeHelp: "分别记录实际转出和到账数量，二者必须为不同资产；可以是同一个账户。",
    feeHelp: "手续费作为额外支出单独记录，不计入本金拆分。可使用其他账户或资产，最多 20 项。", noAccounts: "暂无支持可用资产的账户，请先添加账户或启用账户资产。", noCategories: "暂无可用分类，请先为当前账本添加相应的收入或支出分类。",
    precisionHelp: "最多小数位数", errors: {
      reference: "请选择当前账本内有效的账户、资产和分类。归档或停用的项目不能用于新记账。", date: "请输入有效的交易日期和确认日期。", description: "备注最多 2000 字，不能包含空字符。",
      positive: "本金、分类拆分和手续费必须大于零。", nonzero: "期初余额不能为零。", format: "金额请使用普通十进制文本；不要添加逗号、空格、加号或科学计数法。",
      precision: "金额的小数位数超过所选资产精度。", range: "金额的整数部分最多 20 位。", splits: "收入和支出需要 1 至 100 项分类拆分。", duplicate: "拆分分类不能重复，请合并相同分类的金额。", total: "分类拆分金额合计必须等于本金。", transfer: "转账须选择两个不同的账户，并使用相同资产。", exchange: "换汇须选择不同的转出和转入资产。", fees: "手续费最多 20 项。",
    },
  },
  en: {
    title: "Entry form", kind: "Entry type", account: "Account", asset: "Asset", amount: "Amount", sourceAccount: "Source account", destinationAccount: "Destination account",
    sourceAsset: "Source asset", destinationAsset: "Destination asset", sourceAmount: "Source amount", destinationAmount: "Destination amount", transactionDate: "Transaction date", recognitionDate: "Recognition date", description: "Description",
    choose: "Choose", splitTitle: "Category splits", split: "Split", category: "Category", splitAmount: "Split amount", addSplit: "Add split", removeSplit: "Remove split", feeTitle: "Fees", fee: "Fee",
    feeAccount: "Fee account", feeAsset: "Fee asset", feeAmount: "Fee amount", feeCategory: "Fee category", addFee: "Add fee", removeFee: "Remove fee", save: "Save", saving: "Saving…", cancel: "Cancel", sign: "Toggle sign",
    kinds: { opening: "Opening balance", income: "Income", expense: "Expense", transfer: "Transfer / card repayment", exchange: "Exchange" },
    openingHelp: "An opening balance may be negative but cannot be zero. Each account and asset can have only one opening entry.", splitHelp: "Splits must total the principal amount and use different categories. A single unedited split follows the principal amount.",
    transferHelp: "Move one asset between different accounts in this ledger. Use a transfer for credit-card repayments too.", exchangeHelp: "Enter the quantities actually sent and received in two different assets. Both sides may use the same account.",
    feeHelp: "Fees are additional expenses, separate from principal splits. They may use other accounts or assets. Add up to 20 fees.", noAccounts: "No accounts have an available asset. Add an account or enable its assets first.", noCategories: "No matching categories are available. Add income or expense categories to this ledger first.",
    precisionHelp: "Maximum decimal places", errors: {
      reference: "Choose active accounts, assets and categories in this ledger. Archived or disabled items cannot be used for new entries.", date: "Enter valid transaction and recognition dates.", description: "Use at most 2000 characters without null characters in the description.",
      positive: "Principal amounts, category splits and fees must be greater than zero.", nonzero: "An opening balance cannot be zero.", format: "Use plain decimal amounts without commas, spaces, a plus sign or exponent notation.",
      precision: "An amount has more decimal places than its asset allows.", range: "Amounts can contain at most 20 integer digits.", splits: "Income and expenses require 1 to 100 category splits.", duplicate: "Each split needs a different category. Combine repeated categories.", total: "Category splits must total the principal amount.", transfer: "Select different source and destination accounts holding the same asset.", exchange: "An exchange requires different source and destination assets.", fees: "Add no more than 20 fees.",
    },
  },
};

function localDate(): string {
  const date = new Date();
  return `${date.getFullYear().toString().padStart(4, "0")}-${(date.getMonth() + 1).toString().padStart(2, "0")}-${date.getDate().toString().padStart(2, "0")}`;
}

function validDate(value: string): boolean {
  if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
}

function assetLabel(asset: Asset): string {
  return asset.kind === "token" ? `${asset.code} · ${asset.network} · ${asset.token_reference}` : asset.code;
}

function SelectField({ id, label, value, options, onChange, choose, disabled }: {
  id: string; label: string; value: string; options: Option[]; onChange: (value: string) => void; choose: string; disabled: boolean;
}) {
  return <div className="field"><label htmlFor={id}>{label}</label><select id={id} required value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}><option value="">{choose}</option>{options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>;
}

function AmountField({ id, label, value, onChange, asset, locale, disabled, signed = false }: {
  id: string; label: string; value: string; onChange: (value: string) => void; asset?: Asset; locale: Locale; disabled: boolean; signed?: boolean;
}) {
  const t = copy[locale];
  return <div className="field"><label htmlFor={id}>{label}</label><div className="posting-amount-input"><input id={id} type="text" inputMode="decimal" autoComplete="off" spellCheck={false} required maxLength={40} value={value} disabled={disabled} aria-describedby={asset ? `${id}-help` : undefined} onChange={(event) => onChange(event.target.value)} />{signed && <button type="button" className="secondary-button" disabled={disabled} aria-label={t.sign} onClick={() => onChange(value.startsWith("-") ? value.slice(1) : `-${value}`)}>±</button>}</div>{asset && <p className="help-text" id={`${id}-help`}>{asset.code} · {t.precisionHelp}: {asset.scale}</p>}</div>;
}

export type PostingFormProps = {
  locale: Locale; accounts: Account[]; assets: Asset[]; categories: Category[]; initialKind?: Kind; busy: boolean; error?: string | null;
  onSubmit: (input: PostingInput) => void; onCancel: () => void;
};

export function PostingForm({ locale, accounts, assets, categories, initialKind = "expense", busy, error, onSubmit, onCancel }: PostingFormProps) {
  const t = copy[locale];
  const prefix = useId();
  const enabledAssets = assets.filter((asset) => asset.enabled);
  const activeAccounts = accounts.filter((account) => !account.archived && account.asset_ids.some((id) => enabledAssets.some((asset) => asset.asset_id === id)));
  const [kind, setKind] = useState<Kind>(initialKind);
  const [accountId, setAccountId] = useState(activeAccounts[0]?.id ?? "");
  const [assetId, setAssetId] = useState(enabledAssets.find((asset) => activeAccounts[0]?.asset_ids.includes(asset.asset_id))?.asset_id ?? "");
  const [amount, setAmount] = useState("");
  const [destinationAccountId, setDestinationAccountId] = useState("");
  const [destinationAssetId, setDestinationAssetId] = useState("");
  const [destinationAmount, setDestinationAmount] = useState("");
  const [transactionDate, setTransactionDate] = useState(localDate);
  const [recognitionDate, setRecognitionDate] = useState(localDate);
  const [description, setDescription] = useState("");
  const [splits, setSplits] = useState<SplitDraft[]>(() => [{ id: crypto.randomUUID(), categoryId: "", amount: null }]);
  const [fees, setFees] = useState<FeeDraft[]>([]);
  const [issue, setIssue] = useState<Issue | null>(null);
  const source = activeAccounts.find((account) => account.id === accountId);
  const sourceAsset = enabledAssets.find((asset) => asset.asset_id === assetId);
  const classified = kind === "income" || kind === "expense";
  const twoAccounts = kind === "transfer" || kind === "exchange";
  const currentAccounts = activeAccounts.filter((account) => !source || account.ledger_id === source.ledger_id);
  const categoriesFor = (categoryKind: "income" | "expense") => categories.filter((category) => !category.archived && category.kind === categoryKind && category.ledger_id === source?.ledger_id);
  const relevantCategories = categoriesFor(kind === "income" ? "income" : "expense");
  const expenseCategories = categoriesFor("expense");
  const assetsFor = (id: string) => enabledAssets.filter((asset) => currentAccounts.find((account) => account.id === id)?.asset_ids.includes(asset.asset_id));
  const destinationAccounts = currentAccounts.filter((account) => kind === "transfer" ? account.id !== accountId && account.asset_ids.includes(assetId) : assetsFor(account.id).some((asset) => asset.asset_id !== assetId));
  const destinationAssets = assetsFor(destinationAccountId).filter((asset) => asset.asset_id !== assetId);
  const accountOptions = (items: Account[]): Option[] => items.map((account) => ({ value: account.id, label: account.name }));
  const assetOptions = (items: Asset[]): Option[] => items.map((asset) => ({ value: asset.asset_id, label: assetLabel(asset) }));
  const categoryOptions = (items: Category[]): Option[] => items.map((category) => ({ value: category.id, label: locale === "en" ? category.name_en || category.name : category.name }));
  const clearIssue = () => setIssue(null);

  function selectAccount(value: string) {
    setAccountId(value);
    const next = activeAccounts.find((account) => account.id === value);
    if (!next?.asset_ids.includes(assetId)) setAssetId(enabledAssets.find((asset) => next?.asset_ids.includes(asset.asset_id))?.asset_id ?? "");
    clearIssue();
  }

  function validateQuantity(value: string, asset: Asset | undefined, allowNegative = false): bigint {
    if (!asset) throw { code: "reference" } satisfies Issue;
    let units: bigint;
    try { units = parseAmount(value, asset.scale); }
    catch (problem) {
      const code = problem instanceof AmountError ? problem.code : "amount_format";
      throw { code: code === "amount_precision" ? "precision" : code === "amount_range" ? "range" : "format", asset: asset.code, scale: asset.scale } satisfies Issue;
    }
    if (allowNegative ? units === 0n : units <= 0n) throw { code: allowNegative ? "nonzero" : "positive" } satisfies Issue;
    return units;
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    try {
      if (!source || !assetsFor(accountId).some((asset) => asset.asset_id === assetId)) throw { code: "reference" } satisfies Issue;
      if (!validDate(transactionDate) || (classified && !validDate(recognitionDate))) throw { code: "date" } satisfies Issue;
      if (description.length > 2000 || description.includes("\0")) throw { code: "description" } satisfies Issue;
      const units = validateQuantity(amount, sourceAsset, kind === "opening");
      if (twoAccounts && !destinationAccounts.some((account) => account.id === destinationAccountId)) throw { code: kind === "transfer" ? "transfer" : "reference" } satisfies Issue;
      if (kind === "exchange") {
        if (assetId === destinationAssetId) throw { code: "exchange" } satisfies Issue;
        if (!destinationAssets.some((asset) => asset.asset_id === destinationAssetId)) throw { code: "reference" } satisfies Issue;
        validateQuantity(destinationAmount, enabledAssets.find((asset) => asset.asset_id === destinationAssetId));
      }
      const savedSplits = classified ? splits.map((split) => ({ category_id: split.categoryId, amount: split.amount ?? amount })) : [];
      if (classified) {
        if (!savedSplits.length || savedSplits.length > 100) throw { code: "splits" } satisfies Issue;
        if (new Set(savedSplits.map((split) => split.category_id)).size !== savedSplits.length) throw { code: "duplicate" } satisfies Issue;
        let total = 0n;
        for (const split of savedSplits) {
          if (!relevantCategories.some((category) => category.id === split.category_id)) throw { code: "reference" } satisfies Issue;
          total += validateQuantity(split.amount, sourceAsset);
        }
        if (total !== units) throw { code: "total" } satisfies Issue;
      }
      const savedFees: FeeCreate[] = [];
      if (kind !== "opening") {
        if (fees.length > 20) throw { code: "fees" } satisfies Issue;
        for (const fee of fees) {
          if (!currentAccounts.some((account) => account.id === fee.accountId) || !assetsFor(fee.accountId).some((asset) => asset.asset_id === fee.assetId) || !expenseCategories.some((category) => category.id === fee.categoryId)) throw { code: "reference" } satisfies Issue;
          validateQuantity(fee.amount, enabledAssets.find((asset) => asset.asset_id === fee.assetId));
          savedFees.push({ account_id: fee.accountId, asset_id: fee.assetId, amount: fee.amount, category_id: fee.categoryId });
        }
      }
      const metadata = { transaction_date: transactionDate, description };
      let input: PostingInput;
      if (kind === "opening") input = { kind, body: { ...metadata, account_id: accountId, asset_id: assetId, amount } };
      else if (kind === "income" || kind === "expense") input = { kind, body: { ...metadata, account_id: accountId, asset_id: assetId, amount, recognition_date: recognitionDate, splits: savedSplits, fees: savedFees } };
      else if (kind === "transfer") input = { kind, body: { ...metadata, source_account_id: accountId, destination_account_id: destinationAccountId, asset_id: assetId, amount, fees: savedFees } };
      else input = { kind, body: { ...metadata, source_account_id: accountId, source_asset_id: assetId, source_amount: amount, destination_account_id: destinationAccountId, destination_asset_id: destinationAssetId, destination_amount: destinationAmount, fees: savedFees } };
      setIssue(null);
      onSubmit(input);
    } catch (problem) { setIssue(problem as Issue); }
  }

  return <form className="posting-form" onSubmit={submit} aria-label={t.title} aria-busy={busy}>
    <SelectField id={`${prefix}-kind`} label={t.kind} value={kind} options={(Object.keys(t.kinds) as Kind[]).map((value) => ({ value, label: t.kinds[value] }))} onChange={(value) => { setKind(value as Kind); clearIssue(); }} choose={t.choose} disabled={busy} />
    {!activeAccounts.length && <p className="inline-error" role="status">{t.noAccounts}</p>}
    {(kind === "opening" || twoAccounts) && <p className="help-text posting-intro">{kind === "opening" ? t.openingHelp : kind === "transfer" ? t.transferHelp : t.exchangeHelp}</p>}
    <div className="form-grid">
      <SelectField id={`${prefix}-account`} label={twoAccounts ? t.sourceAccount : t.account} value={accountId} options={accountOptions(activeAccounts)} onChange={selectAccount} choose={t.choose} disabled={busy} />
      <SelectField id={`${prefix}-asset`} label={kind === "exchange" ? t.sourceAsset : t.asset} value={assetId} options={assetOptions(assetsFor(accountId))} onChange={(value) => { setAssetId(value); clearIssue(); }} choose={t.choose} disabled={busy} />
      <AmountField id={`${prefix}-amount`} label={kind === "exchange" ? t.sourceAmount : t.amount} value={amount} onChange={(value) => { setAmount(value); clearIssue(); }} asset={sourceAsset} locale={locale} disabled={busy} signed={kind === "opening"} />
      {twoAccounts && <SelectField id={`${prefix}-destination-account`} label={t.destinationAccount} value={destinationAccountId} options={accountOptions(destinationAccounts)} onChange={(value) => { setDestinationAccountId(value); clearIssue(); }} choose={t.choose} disabled={busy} />}
      {kind === "exchange" && <>
        <SelectField id={`${prefix}-destination-asset`} label={t.destinationAsset} value={destinationAssetId} options={assetOptions(destinationAssets)} onChange={(value) => { setDestinationAssetId(value); clearIssue(); }} choose={t.choose} disabled={busy} />
        <AmountField id={`${prefix}-destination-amount`} label={t.destinationAmount} value={destinationAmount} onChange={(value) => { setDestinationAmount(value); clearIssue(); }} asset={enabledAssets.find((asset) => asset.asset_id === destinationAssetId)} locale={locale} disabled={busy} />
      </>}
      <div className="field"><label htmlFor={`${prefix}-transaction-date`}>{t.transactionDate}</label><input id={`${prefix}-transaction-date`} type="date" required value={transactionDate} disabled={busy} onChange={(event) => setTransactionDate(event.target.value)} /></div>
      {classified && <div className="field"><label htmlFor={`${prefix}-recognition-date`}>{t.recognitionDate}</label><input id={`${prefix}-recognition-date`} type="date" required value={recognitionDate} disabled={busy} onChange={(event) => setRecognitionDate(event.target.value)} /></div>}
    </div>
    {classified && <section className="form-section"><h3>{t.splitTitle}</h3><p className="help-text">{t.splitHelp}</p>{!relevantCategories.length && <p className="help-text">{t.noCategories}</p>}
      {splits.map((split, index) => <fieldset className="posting-component" data-testid="split-row" key={split.id} disabled={busy}><legend>{t.split} {index + 1}</legend><div className="form-grid">
        <SelectField id={`${prefix}-split-category-${split.id}`} label={t.category} value={split.categoryId} options={categoryOptions(relevantCategories.filter((category) => category.id === split.categoryId || !splits.some((other) => other.categoryId === category.id)))} onChange={(value) => { setSplits(splits.map((row) => row.id === split.id ? { ...row, categoryId: value } : row)); clearIssue(); }} choose={t.choose} disabled={busy} />
        <AmountField id={`${prefix}-split-amount-${split.id}`} label={t.splitAmount} value={split.amount ?? amount} onChange={(value) => { setSplits(splits.map((row) => row.id === split.id ? { ...row, amount: value } : row)); clearIssue(); }} asset={sourceAsset} locale={locale} disabled={busy} />
      </div><button type="button" className="secondary-button" disabled={busy || splits.length <= 1} aria-label={`${t.removeSplit} ${index + 1}`} onClick={() => { setSplits(splits.filter((row) => row.id !== split.id)); clearIssue(); }}>{t.removeSplit}</button></fieldset>)}
      <button type="button" className="secondary-button" disabled={busy || splits.length >= 100 || splits.length >= relevantCategories.length} onClick={() => { setSplits([...splits.map((split) => ({ ...split, amount: split.amount ?? amount })), { id: crypto.randomUUID(), categoryId: "", amount: "" }]); clearIssue(); }}>{t.addSplit}</button>
    </section>}
    {kind !== "opening" && <section className="form-section"><h3>{t.feeTitle}</h3><p className="help-text">{t.feeHelp}</p>
      {fees.map((fee, index) => <fieldset className="posting-component" data-testid="fee-row" key={fee.id} disabled={busy}><legend>{t.fee} {index + 1}</legend><div className="form-grid">
        <SelectField id={`${prefix}-fee-account-${fee.id}`} label={t.feeAccount} value={fee.accountId} options={accountOptions(currentAccounts)} onChange={(value) => { setFees(fees.map((row) => row.id === fee.id ? { ...row, accountId: value, assetId: assetsFor(value).some((asset) => asset.asset_id === row.assetId) ? row.assetId : "" } : row)); clearIssue(); }} choose={t.choose} disabled={busy} />
        <SelectField id={`${prefix}-fee-asset-${fee.id}`} label={t.feeAsset} value={fee.assetId} options={assetOptions(assetsFor(fee.accountId))} onChange={(value) => { setFees(fees.map((row) => row.id === fee.id ? { ...row, assetId: value } : row)); clearIssue(); }} choose={t.choose} disabled={busy} />
        <AmountField id={`${prefix}-fee-amount-${fee.id}`} label={t.feeAmount} value={fee.amount} onChange={(value) => { setFees(fees.map((row) => row.id === fee.id ? { ...row, amount: value } : row)); clearIssue(); }} asset={enabledAssets.find((asset) => asset.asset_id === fee.assetId)} locale={locale} disabled={busy} />
        <SelectField id={`${prefix}-fee-category-${fee.id}`} label={t.feeCategory} value={fee.categoryId} options={categoryOptions(expenseCategories)} onChange={(value) => { setFees(fees.map((row) => row.id === fee.id ? { ...row, categoryId: value } : row)); clearIssue(); }} choose={t.choose} disabled={busy} />
      </div><button type="button" className="secondary-button" disabled={busy} aria-label={`${t.removeFee} ${index + 1}`} onClick={() => { setFees(fees.filter((row) => row.id !== fee.id)); clearIssue(); }}>{t.removeFee}</button></fieldset>)}
      <button type="button" className="secondary-button" disabled={busy || fees.length >= 20 || !currentAccounts.length || !expenseCategories.length} onClick={() => { setFees([...fees, { id: crypto.randomUUID(), accountId, assetId, amount: "", categoryId: "" }]); clearIssue(); }}>{t.addFee}</button>
    </section>}
    <div className="field"><label htmlFor={`${prefix}-description`}>{t.description}</label><textarea id={`${prefix}-description`} maxLength={2000} value={description} disabled={busy} onChange={(event) => setDescription(event.target.value)} /></div>
    {(issue || error) && <p className="inline-error" role="alert">{issue ? <>{t.errors[issue.code]}{issue.asset && ` ${issue.asset} · ${t.precisionHelp}: ${issue.scale}`}</> : error}</p>}
    <div className="form-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onCancel}>{t.cancel}</button><button type="submit" className="primary-button" disabled={busy || !activeAccounts.length}>{busy ? t.saving : t.save}</button></div>
  </form>;
}
