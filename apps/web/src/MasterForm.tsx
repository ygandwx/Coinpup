import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Locale } from "./i18n";
import type { MasterKind, PartyCreate, PartyUpdate, ProjectUpdate } from "./business-api";
import { masterName } from "./pending-business";
import { containsControlCharacters } from "./regions";

export type MasterFields = Partial<PartyUpdate & ProjectUpdate>;
const optionalFields = [
    ["legal_name", "法定名称", "Legal name", 200],
    ["email", "邮箱", "Email", 254],
    ["phone", "电话", "Phone", 64],
    ["address", "地址", "Address", 1000],
    ["tax_identifier", "税务编号", "Tax identifier", 128],
    ["notes", "备注", "Notes", 2000],
] as const;
export function MasterForm({
    initial,
    kind,
    locale,
    locked,
    canFinish,
    existing,
    onSave,
    onCancel,
}: {
    initial: MasterFields;
    kind: MasterKind;
    locale: Locale;
    locked: boolean;
    canFinish: boolean;
    existing: boolean;
    onSave: (fields: Omit<PartyCreate, "id"> & { archived?: boolean }) => void;
    onCancel: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const id = useId();
    const [values, setValues] = useState<MasterFields>(initial);
    const [invalid, setInvalid] = useState(false);
    function submit(event: FormEvent) {
        event.preventDefault();
        if (locked) return;
        const name = masterName(values.name ?? "");
        const legal = masterName(values.legal_name ?? "");
        if (
            !name ||
            containsControlCharacters(name) ||
            (legal && containsControlCharacters(legal)) ||
            optionalFields.some(([field]) => values[field]?.includes("\0"))
        ) {
            setInvalid(true);
            return;
        }
        setInvalid(false);
        const fields = Object.fromEntries(
            optionalFields
                .filter(([field]) => kind === "parties" || field === "notes")
                .map(([field]) => [
                    field,
                    values[field] === "" || values[field] == null ? null : values[field],
                ]),
        );
        onSave({
            name: values.name!,
            role: values.role ?? "both",
            ...fields,
            ...(existing ? { archived: values.archived ?? false } : {}),
        });
    }
    return (
        <form
            className="editor-form"
            aria-label={t("编辑资料", "Edit master data")}
            onSubmit={submit}
        >
            <fieldset disabled={locked}>
                <legend>{t("资料内容", "Record details")}</legend>
                <div className="form-grid">
                    <div className="master-field">
                        <label htmlFor={`${id}-name`}>{t("名称", "Name")}</label>
                        <input
                            id={`${id}-name`}
                            required
                            maxLength={160}
                            value={values.name ?? ""}
                            onChange={(e) => setValues({ ...values, name: e.target.value })}
                        />
                    </div>
                    {kind === "parties" && (
                        <div className="master-field">
                            <label htmlFor={`${id}-role`}>{t("往来角色", "Party role")}</label>
                            <select
                                id={`${id}-role`}
                                value={values.role ?? "both"}
                                onChange={(e) =>
                                    setValues({
                                        ...values,
                                        role: e.target.value as PartyCreate["role"],
                                    })
                                }
                            >
                                <option value="customer">{t("客户", "Customer")}</option>
                                <option value="supplier">{t("供应商", "Supplier")}</option>
                                <option value="both">
                                    {t("客户与供应商", "Customer and supplier")}
                                </option>
                            </select>
                        </div>
                    )}
                    {optionalFields
                        .filter(([field]) => kind === "parties" || field === "notes")
                        .map(([field, zh, en, max]) => (
                            <div className="master-field" key={field}>
                                <label htmlFor={`${id}-${field}`}>{t(zh, en)}</label>
                                <textarea
                                    id={`${id}-${field}`}
                                    maxLength={max}
                                    rows={field === "notes" || field === "address" ? 3 : 1}
                                    value={values[field] ?? ""}
                                    onChange={(e) =>
                                        setValues({ ...values, [field]: e.target.value })
                                    }
                                />
                            </div>
                        ))}
                </div>
                {existing && (
                    <label className="checkbox-label">
                        <input
                            type="checkbox"
                            checked={values.archived ?? false}
                            onChange={(e) => setValues({ ...values, archived: e.target.checked })}
                        />
                        {t("已归档", "Archived")}
                    </label>
                )}
            </fieldset>
            {invalid && (
                <p role="alert">
                    {t(
                        "请输入非空名称，并移除无效控制字符。",
                        "Enter a nonempty name and remove invalid control characters.",
                    )}
                </p>
            )}
            <div className="form-actions">
                <button type="submit" className="primary-button" disabled={locked}>
                    {t("保存资料", "Save record")}
                </button>
                <button type="button" disabled={!canFinish} onClick={onCancel}>
                    {t("结束编辑", "Finish editing")}
                </button>
            </div>
        </form>
    );
}
