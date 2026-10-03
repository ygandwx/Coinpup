import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { Asset, Entity, EntityCreateBody, EntityUpdateBody } from "./ledger-api";
import { COMPANY_CONTACT_FIELDS, REGIONS, REGION_SOURCES, compileDetails, detailRows, replaceDetail } from "./regions";
import type { CountryCode, DetailField, DetailRow } from "./regions";

const labels = {
  zh: {
    entityType: "主体类型", personal: "个人", company: "公司", name: "显示名称", country: "注册国家／地区", state: "州",
    companyType: "公司类型", legalName: "法定名称", date: "成立／登记日期", base: "基础币种", template: "分类模板",
    personalTemplate: "个人常用分类", businessTemplate: "公司常用分类", noTemplate: "不添加模板分类", profile: "公司资料",
    optional: "以下资料均可稍后补填。编号按证件原文保存。", sources: "官方资料", custom: "自定义资料", customKey: "字段名称",
    customValue: "字段内容", add: "添加自定义字段", remove: "移除字段", save: "保存", saving: "保存中…", cancel: "取消",
    locked: "主体类型和基础币种创建后固定。", baseHelp: "基础币种用于账本设置；账户可同时持有多种资产。",
    customHelp: "已有资料会保留。自定义字段名称不可重复或占用预设字段名。最多保存 32 项资料。",
    detailsError: "请检查资料：字段名称须唯一、不能占用预设字段名，名称最长 64 字、内容最长 2000 字，最多 32 项。",
    nameError: "请输入有效的显示名称，名称不能只包含空格或控制字符。", assetsError: "请选择可用的法定货币作为基础币种。",
  },
  en: {
    entityType: "Entity type", personal: "Personal", company: "Company", name: "Display name", country: "Country / region", state: "State",
    companyType: "Company type", legalName: "Legal name", date: "Registration date", base: "Base currency", template: "Category template",
    personalTemplate: "Personal categories", businessTemplate: "Business categories", noTemplate: "No template categories", profile: "Company details",
    optional: "All details below can be added later. Copy identifiers as issued.", sources: "Official sources", custom: "Custom details", customKey: "Field name",
    customValue: "Field value", add: "Add custom field", remove: "Remove field", save: "Save", saving: "Saving…", cancel: "Cancel",
    locked: "Entity type and base currency are fixed after creation.", baseHelp: "The base currency is a ledger setting; accounts can hold multiple assets.",
    customHelp: "Existing details are preserved. Custom keys must be unique and cannot reuse preset field names. Save up to 32 details.",
    detailsError: "Check details: use unique, non-reserved names of up to 64 characters, values of up to 2000 characters, and no more than 32 fields.",
    nameError: "Enter a display name without control characters; spaces alone are not a name.", assetsError: "Select an available fiat asset as the base currency.",
  },
};

const COMPANY_KEYS = new Set([...COMPANY_CONTACT_FIELDS, ...Object.values(REGIONS).flatMap((region) => region.fields)].map((field) => field.key));

export function DetailEditor({ locale, rows, fields, busy, onChange, prefix }: {
  locale: Locale; rows: DetailRow[]; fields: readonly DetailField[]; busy: boolean;
  onChange: (rows: DetailRow[]) => void; prefix: string;
}) {
  const t = labels[locale];
  const visibleKeys = new Set(fields.map((field) => field.key));
  const customRows = rows.filter((row) => row.custom || !visibleKeys.has(row.key));
  return <>
    {fields.length > 0 && <div className="form-grid">{fields.map((field) => <div className="field" key={field.key}>
      <label htmlFor={`${prefix}-${field.key}`}>{field.label[locale]}</label>
      <input id={`${prefix}-${field.key}`} value={rows.find((row) => !row.custom && row.key === field.key)?.value ?? ""} maxLength={2000} disabled={busy} onChange={(event) => onChange(replaceDetail(rows, field.key, event.target.value))} aria-describedby={field.help ? `${prefix}-${field.key}-help` : undefined} />
      {field.help && <p className="help-text" id={`${prefix}-${field.key}-help`}>{field.help[locale]}</p>}
    </div>)}</div>}
    <section className="form-section" aria-labelledby={`${prefix}-custom-title`}>
      <h3 id={`${prefix}-custom-title`}>{t.custom}</h3>
      <p className="help-text">{t.customHelp}</p>
      {customRows.map((row, index) => <div className="form-grid" key={row.id}>
        <div className="field"><label htmlFor={`${prefix}-key-${row.id}`}>{t.customKey} {index + 1}</label><input id={`${prefix}-key-${row.id}`} value={row.key} maxLength={64} disabled={busy} onChange={(event) => onChange(rows.map((item) => item.id === row.id ? { ...item, key: event.target.value, custom: true } : item))} /></div>
        <div className="field"><label htmlFor={`${prefix}-value-${row.id}`}>{t.customValue} {index + 1}</label><input id={`${prefix}-value-${row.id}`} value={row.value} maxLength={2000} disabled={busy} onChange={(event) => onChange(rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item))} /></div>
        <button type="button" className="secondary-button" disabled={busy} aria-label={`${t.remove} ${index + 1}`} onClick={() => onChange(rows.filter((item) => item.id !== row.id))}>{t.remove}</button>
      </div>)}
      <button type="button" className="secondary-button" disabled={busy || rows.filter((row) => row.value || row.key).length >= 32} onClick={() => onChange([...rows, { id: crypto.randomUUID(), key: "", value: "", custom: true }])}>{t.add}</button>
    </section>
  </>;
}

export type EntityFormProps = {
  locale: Locale; assets: Asset[]; entity?: Entity; busy: boolean; error?: string | null;
  onSubmit: (body: EntityCreateBody | EntityUpdateBody) => void; onCancel: () => void;
};

export function EntityForm({ locale, assets, entity, busy, error, onSubmit, onCancel }: EntityFormProps) {
  const t = labels[locale];
  const prefix = useId();
  const [ids] = useState(() => ({ id: crypto.randomUUID(), ledger_id: crypto.randomUUID() }));
  const [expectedVersion] = useState(entity?.version);
  const [kind, setKind] = useState<"personal" | "company">(entity?.kind ?? "personal");
  const [name, setName] = useState(entity?.name ?? "");
  const [country, setCountry] = useState<CountryCode>((entity?.country_code as CountryCode | null) ?? "CN");
  const [state, setState] = useState<"NM" | "WY">(entity?.region_code === "WY" ? "WY" : "NM");
  const [legalName, setLegalName] = useState(entity?.legal_name ?? "");
  const [date, setDate] = useState(entity?.registration_date ?? "");
  const [base, setBase] = useState(entity?.ledger.base_asset_id ?? assets.find((asset) => asset.enabled && asset.kind === "fiat" && asset.code === "USD")?.asset_id ?? assets.find((asset) => asset.enabled && asset.kind === "fiat")?.asset_id ?? "");
  const [template, setTemplate] = useState<"personal_default" | "business_default" | "">("personal_default");
  const [rows, setRows] = useState(() => detailRows(entity?.details ?? {}, COMPANY_KEYS));
  const [localError, setLocalError] = useState<"name" | "assets" | "details" | null>(null);
  const region = REGIONS[country];
  const fields = kind === "company" ? [...region.fields, ...COMPANY_CONTACT_FIELDS] : [];
  const fiatAssets = assets.filter((asset) => asset.kind === "fiat" && (asset.enabled || asset.asset_id === entity?.ledger.base_asset_id));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    if (!name.trim() || /[\u0000-\u001f]/.test(name) || (kind === "company" && legalName && (!legalName.trim() || /[\u0000-\u001f]/.test(legalName)))) { setLocalError("name"); return; }
    if (!entity && !fiatAssets.some((asset) => asset.asset_id === base && asset.enabled)) { setLocalError("assets"); return; }
    let details: Record<string, string>;
    try { details = compileDetails(rows, COMPANY_KEYS); } catch { setLocalError("details"); return; }
    setLocalError(null);
    const profile = {
      name: name.trim(), details,
      legal_name: kind === "company" ? legalName.trim() || null : null,
      country_code: kind === "company" ? country : null,
      region_code: kind === "company" && country === "US" ? state : null,
      company_type: kind === "company" ? region.companyType : null,
      registration_date: kind === "company" ? date || null : null,
    };
    if (entity && expectedVersion !== undefined) onSubmit({ ...profile, expected_version: expectedVersion });
    else onSubmit({ ...ids, ...profile, kind, base_asset_id: base, template_key: template || null, locale });
  }

  return <form onSubmit={submit} aria-busy={busy} aria-label={locale === "zh" ? "主体资料表单" : "Entity details form"}>
    <div className="form-grid">
      <div className="field"><label htmlFor={`${prefix}-kind`}>{t.entityType}</label><select id={`${prefix}-kind`} value={kind} disabled={busy || !!entity} onChange={(event) => { const value = event.target.value as "personal" | "company"; setKind(value); setTemplate((current) => current ? value === "personal" ? "personal_default" : "business_default" : ""); }}><option value="personal">{t.personal}</option><option value="company">{t.company}</option></select></div>
      <div className="field"><label htmlFor={`${prefix}-name`}>{t.name}</label><input id={`${prefix}-name`} required maxLength={160} value={name} disabled={busy} onChange={(event) => { setName(event.target.value); setLocalError(null); }} /></div>
      {kind === "company" && <>
        <div className="field"><label htmlFor={`${prefix}-country`}>{t.country}</label><select id={`${prefix}-country`} value={country} disabled={busy} onChange={(event) => setCountry(event.target.value as CountryCode)}>{(Object.keys(REGIONS) as CountryCode[]).map((key) => <option key={key} value={key}>{REGIONS[key].name[locale]}</option>)}</select></div>
        {country === "US" && <div className="field"><label htmlFor={`${prefix}-state`}>{t.state}</label><select id={`${prefix}-state`} value={state} disabled={busy} onChange={(event) => setState(event.target.value as "NM" | "WY")}><option value="NM">{locale === "zh" ? "新墨西哥州" : "New Mexico"} (NM)</option><option value="WY">{locale === "zh" ? "怀俄明州" : "Wyoming"} (WY)</option></select></div>}
        <div className="field"><label htmlFor={`${prefix}-company-type`}>{t.companyType}</label><input id={`${prefix}-company-type`} value={region.companyTypeLabel[locale]} readOnly /></div>
        <div className="field"><label htmlFor={`${prefix}-legal-name`}>{t.legalName}</label><input id={`${prefix}-legal-name`} maxLength={160} value={legalName} disabled={busy} onChange={(event) => setLegalName(event.target.value)} /></div>
        <div className="field"><label htmlFor={`${prefix}-date`}>{t.date}</label><input id={`${prefix}-date`} type="date" value={date} disabled={busy} onChange={(event) => setDate(event.target.value)} /></div>
      </>}
      <div className="field"><label htmlFor={`${prefix}-base`}>{t.base}</label><select id={`${prefix}-base`} value={base} required disabled={busy || !!entity} onChange={(event) => setBase(event.target.value)}><option value="">—</option>{entity && !fiatAssets.some((asset) => asset.asset_id === base) && <option value={base}>{base}</option>}{fiatAssets.map((asset) => <option key={asset.asset_id} value={asset.asset_id}>{asset.code}</option>)}</select><p className="help-text">{entity ? t.locked : t.baseHelp}</p></div>
      {!entity && <div className="field"><label htmlFor={`${prefix}-template`}>{t.template}</label><select id={`${prefix}-template`} value={template} disabled={busy} onChange={(event) => setTemplate(event.target.value as typeof template)}><option value="personal_default">{t.personalTemplate}</option><option value="business_default">{t.businessTemplate}</option><option value="">{t.noTemplate}</option></select></div>}
    </div>
    {kind === "company" && <section className="form-section"><h3>{t.profile}</h3><p className="help-text">{region.introduction[locale]}</p><p className="help-text">{t.optional}</p><p className="help-text">{t.sources}: {REGION_SOURCES[country].map((source, index) => <span key={source.url}>{index > 0 ? " · " : ""}<a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></span>)}</p></section>}
    <DetailEditor locale={locale} rows={rows} fields={fields} busy={busy} onChange={setRows} prefix={`${prefix}-details`} />
    {(localError || error) && <p className="inline-error" role="alert">{localError ? t[`${localError}Error`] : error}</p>}
    <div className="form-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onCancel}>{t.cancel}</button><button type="submit" className="primary-button" disabled={busy}>{busy ? t.saving : t.save}</button></div>
  </form>;
}
