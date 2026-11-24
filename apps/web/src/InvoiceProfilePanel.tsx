import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ApiError, readJson } from "./api";
import type { Session } from "./api";
import { listFiles } from "./files-api";
import type { StoredFile } from "./files-api";
import { getInvoiceProfile } from "./invoice-api";
import { validReviewedInvoiceProfile } from "./invoice-web-intent";
import type { InvoiceProfile } from "./invoice-issuance-receipt";
import type { PendingInvoiceProfiles } from "./pending-invoice-profiles";
import type { Locale } from "./i18n";
import { uuid } from "./credit-intent";

type Form = {
    id: string;
    previous: InvoiceProfile | null;
    name: string;
    prefix: string;
    digits: string;
    issuer: string;
    payment: string;
    footer: string;
    logoId: string;
};
const editor = (p: InvoiceProfile | null): Form => ({
    id: p?.id ?? crypto.randomUUID(),
    previous: p,
    name: p?.name ?? "",
    prefix: p?.numbering.prefix ?? "",
    digits: String(p?.numbering.minimum_digits ?? 4),
    issuer: p?.issuer_extra_text ?? "",
    payment: p?.payment_instructions ?? "",
    footer: p?.footer ?? "",
    logoId: p?.logo_file_id ?? "",
});
type Props = {
    ledgerId: string;
    locale: Locale;
    session: Session;
    archived: boolean;
    controller: PendingInvoiceProfiles;
    onUnauthorized: () => void;
    onEditing: (v: boolean) => void;
    onClose: () => void;
};
export function InvoiceProfilePanel({
    ledgerId,
    locale,
    session,
    archived,
    controller,
    onUnauthorized,
    onEditing,
    onClose,
}: Props) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en),
        state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
    const [form, setForm] = useState<Form | null>(null),
        [page, setPage] = useState(0),
        [filesPage, setFilesPage] = useState(0),
        [logo, setLogo] = useState<StoredFile | null>(null);
    const [message, setMessage] = useState(false),
        [refresh, setRefresh] = useState(0),
        [selection, setSelection] = useState<string | null>(null);
    const plan =
        state.plan?.ownerId === session.user.id && state.plan.ledgerId === ledgerId
            ? state.plan
            : null;
    const editing = form !== null;
    const nameTooLong = form !== null && Array.from(form.name).length > 160;
    const retainedLogo =
        form?.previous && form.logoId === form.previous.logo_file_id && !logo
            ? form.previous
            : null;
    const selectedLogoVersion = form?.logoId
        ? (retainedLogo?.logo_file_version ?? logo?.version ?? null)
        : null;
    const key = useMemo(
        () => ({ session, ledgerId, page, filesPage, editing, plan, refresh, selection }),
        [session, ledgerId, page, filesPage, editing, plan, refresh, selection],
    );
    const latest = useRef({ key, onUnauthorized, onEditing });
    latest.current = { key, onUnauthorized, onEditing };
    const [loaded, setLoaded] = useState<{
        key: typeof key;
        profiles?: InvoiceProfile[];
        files?: StoredFile[];
        more?: boolean;
        failed?: boolean;
    } | null>(null);
    useEffect(() => {
        latest.current.onEditing(true);
        return () => latest.current.onEditing(false);
    }, []);
    useEffect(() => {
        if (state.status === "auth-required") latest.current.onUnauthorized();
    }, [state.status]);
    useEffect(() => {
        if (plan) return;
        const a = new AbortController();
        const alive = () => latest.current.key === key && !a.signal.aborted;
        async function read() {
            if (key.editing) {
                const rows = await listFiles(
                    ledgerId,
                    { limit: 50, offset: filesPage * 50, include_archived: false },
                    a.signal,
                );
                if (!alive()) return;
                if (
                    !Array.isArray(rows) ||
                    rows.length > 50 ||
                    !rows.every(
                        (r) =>
                            r &&
                            r.ledger_id === ledgerId &&
                            uuid(r.id) &&
                            Number.isInteger(r.version) &&
                            r.version > 0 &&
                            !r.archived &&
                            typeof r.original_filename === "string",
                    ) ||
                    new Set(rows.map((r) => r.id)).size !== rows.length
                )
                    throw new Error();
                setLoaded({
                    key,
                    files: rows.filter((r) =>
                        ["image/png", "image/jpeg", "image/webp"].includes(r.detected_media_type),
                    ),
                    more: rows.length === 50,
                });
            } else if (selection) {
                const p = await getInvoiceProfile(ledgerId, selection, a.signal);
                if (!alive()) return;
                if (!validReviewedInvoiceProfile(p, ledgerId) || p.id !== selection)
                    throw new Error();
                setLogo(null);
                setFilesPage(0);
                setForm(editor(p));
                setSelection(null);
            } else {
                const path = `/api/v1/ledgers/${encodeURIComponent(ledgerId)}/invoice-profiles?include_archived=true&limit=50&offset=${page * 50}`;
                const rows = await readJson<unknown>(path, a.signal);
                if (!alive()) return;
                if (
                    !Array.isArray(rows) ||
                    rows.length > 50 ||
                    !rows.every((r) => validReviewedInvoiceProfile(r, ledgerId)) ||
                    new Set(rows.map((r) => r.id)).size !== rows.length
                )
                    throw new Error();
                setLoaded({ key, profiles: rows, more: rows.length === 50 });
            }
        }
        void read().catch((e) => {
            if (alive()) {
                setLoaded({ key, failed: true });
                if (e instanceof ApiError && e.kind === "unauthorized")
                    latest.current.onUnauthorized();
            }
        });
        return () => a.abort();
    }, [key, ledgerId, filesPage, page, selection, plan]);
    const data = loaded?.key === key ? loaded : null;
    const update = (k: keyof Omit<Form, "id" | "previous">, value: string) => {
        setForm((f) => (f ? { ...f, [k]: value } : f));
        setMessage(false);
    };
    function save(action: "save" | "archive") {
        if (!form || archived || plan) return;
        const p = form.previous;
        const body = {
            name: form.name,
            numbering: {
                prefix: form.prefix,
                minimum_digits: /^(?:[1-9]|1[0-9]|20)$/u.test(form.digits)
                    ? parseInt(form.digits, 10)
                    : 0,
            },
            issuer_extra_text: form.issuer || null,
            footer: form.footer || null,
            payment_instructions: form.payment || null,
            logo_file_id: form.logoId || null,
            logo_file_version: selectedLogoVersion,
        };
        setMessage(false);
        const promise =
            action === "archive" && p
                ? controller.start(
                      session,
                      ledgerId,
                      form.id,
                      "archive",
                      { expected_version: p.version, archived: !p.archived },
                      p,
                  )
                : p
                  ? controller.start(
                        session,
                        ledgerId,
                        form.id,
                        "replace",
                        { ...body, expected_version: p.version },
                        p,
                        retainedLogo ? null : logo,
                    )
                  : controller.start(
                        session,
                        ledgerId,
                        form.id,
                        "create",
                        { ...body, id: form.id },
                        null,
                        logo,
                    );
        void promise.catch((e) => {
            if (latest.current.key.session !== session) return;
            if (e instanceof ApiError && e.kind === "unauthorized") latest.current.onUnauthorized();
            else setMessage(true);
        });
    }
    function reset() {
        setForm(null);
        setSelection(null);
        setLogo(null);
        setFilesPage(0);
        setMessage(false);
        setRefresh((n) => n + 1);
    }
    if (state.status === "auth-required" || (state.plan && !plan)) return null;
    return (
        <section
            className="business-panel master-panel invoice-panel"
            aria-label={t("签发配置管理", "Manage issuance profiles")}
        >
            <h3>{t("签发配置管理", "Manage issuance profiles")}</h3>
            {plan ? (
                <>
                    <p role="status">
                        {state.status === "confirmed"
                            ? t("配置已核对并保存。", "Profile verified and saved.")
                            : state.status === "conflict"
                              ? t(
                                    "当前配置版本已变化。请核对后显式重新载入。",
                                    "The current profile differs. Review it and explicitly reload before editing.",
                                )
                              : state.status === "rejected"
                                ? t(
                                      "配置未保存，请重新审阅。",
                                      "Profile was not saved. Review it again.",
                                  )
                                : state.status === "submitting"
                                  ? t("正在保存配置…", "Saving the profile…")
                                  : t(
                                        "保存结果未知，请重试原请求。",
                                        "Save result unknown. Retry the original request.",
                                    )}
                    </p>
                    {state.record && (
                        <p>
                            {state.record.name} · {t("版本", "Version")} {state.record.version} ·{" "}
                            {state.record.numbering.prefix} /{" "}
                            {state.record.numbering.minimum_digits}
                        </p>
                    )}
                    {state.status === "unknown" && (
                        <button
                            className="secondary-button"
                            onClick={() => {
                                void controller.retry(session).catch(() => setMessage(true));
                            }}
                        >
                            {t("重试原配置请求", "Retry original profile request")}
                        </button>
                    )}
                    {state.status === "conflict" && state.record && (
                        <button
                            className="secondary-button"
                            onClick={() => {
                                const id = plan.id;
                                if (controller.dismiss()) {
                                    reset();
                                    setSelection(id);
                                }
                            }}
                        >
                            {t("载入当前配置", "Load current profile")}
                        </button>
                    )}
                    {["confirmed", "rejected", "conflict"].includes(state.status) && (
                        <button
                            className="secondary-button"
                            onClick={() => {
                                if (controller.dismiss()) reset();
                            }}
                        >
                            {t("返回配置列表", "Back to profile list")}
                        </button>
                    )}
                </>
            ) : form ? (
                <>
                    <h4>
                        {form.previous
                            ? t("编辑配置", "Edit profile")
                            : t("创建配置", "Create profile")}
                    </h4>
                    <label>
                        {t("配置名称", "Profile name")}
                        <input
                            value={form.name}
                            aria-invalid={nameTooLong || undefined}
                            aria-describedby={
                                nameTooLong ? "invoice-profile-name-limit" : undefined
                            }
                            onChange={(e) => update("name", e.target.value)}
                        />
                    </label>
                    {nameTooLong && (
                        <p id="invoice-profile-name-limit" role="alert">
                            {t(
                                "配置名称不能超过 160 个字符。",
                                "Use at most 160 characters for the profile name.",
                            )}
                        </p>
                    )}
                    <label>
                        {t("号码前缀", "Number prefix")}
                        <input
                            value={form.prefix}
                            onChange={(e) => update("prefix", e.target.value)}
                        />
                    </label>
                    <label>
                        {t("最少数字位数", "Minimum digits")}
                        <input
                            value={form.digits}
                            inputMode="numeric"
                            onChange={(e) => update("digits", e.target.value)}
                        />
                    </label>
                    {(
                        [
                            ["issuer", "发行人补充资料", "Additional issuer details"],
                            ["payment", "收款说明", "Payment instructions"],
                            ["footer", "页脚", "Footer"],
                        ] as const
                    ).map(([field, zh, en]) => (
                        <label key={field}>
                            {t(zh, en)}
                            <textarea
                                value={form[field]}
                                onChange={(e) => update(field, e.target.value)}
                            />
                        </label>
                    ))}
                    <label>
                        {t("账本内 Logo 原件", "Private ledger logo")}
                        <select
                            value={form.logoId}
                            onChange={(e) => {
                                const id = e.target.value;
                                setLogo(data?.files?.find((r) => r.id === id) ?? null);
                                update("logoId", id);
                            }}
                        >
                            <option value="">{t("无 Logo", "No logo")}</option>
                            {form.logoId && !data?.files?.some((f) => f.id === form.logoId) && (
                                <option value={form.logoId}>
                                    {logo?.original_filename ?? form.logoId}
                                    {selectedLogoVersion !== null && ` · v${selectedLogoVersion}`}
                                </option>
                            )}
                            {data?.files?.map((f) => {
                                const version =
                                    f.id === form.logoId ? selectedLogoVersion : f.version;
                                return (
                                    <option key={f.id} value={f.id}>
                                        {f.original_filename}
                                        {version !== null && ` · v${version}`}
                                    </option>
                                );
                            })}
                        </select>
                    </label>
                    <button
                        className="secondary-button"
                        disabled={!filesPage}
                        onClick={() => setFilesPage((n) => n - 1)}
                    >
                        {t("上一页文件", "Previous files")}
                    </button>
                    <button
                        className="secondary-button"
                        disabled={!data?.more || filesPage >= 2000}
                        onClick={() => setFilesPage((n) => n + 1)}
                    >
                        {t("下一页文件", "Next files")}
                    </button>
                    <p>
                        {t(
                            "新 Logo 请先在本账本的票据与证件中上传。",
                            "Upload a new logo in this ledger’s Documents first.",
                        )}
                    </p>
                    {!archived && (
                        <button
                            className="secondary-button"
                            disabled={nameTooLong}
                            onClick={() => save("save")}
                        >
                            {t("保存配置", "Save profile")}
                        </button>
                    )}
                    {form.previous && !archived && (
                        <button className="secondary-button" onClick={() => save("archive")}>
                            {form.previous.archived
                                ? t("恢复配置", "Restore profile")
                                : t("归档配置", "Archive profile")}
                        </button>
                    )}
                    <button className="secondary-button" onClick={reset}>
                        {t("返回配置列表", "Back to profile list")}
                    </button>
                </>
            ) : (
                <>
                    {data?.profiles?.map((p) => (
                        <p key={p.id}>
                            <button className="secondary-button" onClick={() => setSelection(p.id)}>
                                {p.name}
                            </button>{" "}
                            · {t("版本", "Version")} {p.version}
                            {p.archived ? " · " + t("已归档", "Archived") : ""}
                        </p>
                    ))}
                    {data?.profiles?.length === 0 && (
                        <p>{t("尚无签发配置。", "There are no issuance profiles.")}</p>
                    )}
                    {!data && !plan && (
                        <p role="status">{t("正在读取配置…", "Loading profiles…")}</p>
                    )}
                    <button
                        className="secondary-button"
                        disabled={!page}
                        onClick={() => setPage((n) => n - 1)}
                    >
                        {t("上一页配置", "Previous profiles")}
                    </button>
                    <button
                        className="secondary-button"
                        disabled={!data?.more || page >= 2000}
                        onClick={() => setPage((n) => n + 1)}
                    >
                        {t("下一页配置", "Next profiles")}
                    </button>
                    {!archived && (
                        <button
                            className="secondary-button"
                            onClick={() => {
                                reset();
                                setForm(editor(null));
                            }}
                        >
                            {t("创建配置", "Create profile")}
                        </button>
                    )}
                    <button className="secondary-button" onClick={onClose}>
                        {t("关闭", "Close")}
                    </button>
                </>
            )}
            {data?.failed && !plan && (
                <p role="alert">
                    {t("读取失败，请重试。", "Read failed. Please retry.")}{" "}
                    <button className="secondary-button" onClick={() => setRefresh((n) => n + 1)}>
                        {t("重试读取", "Retry read")}
                    </button>
                </p>
            )}
            {message && (
                <p role="alert">
                    {t(
                        "请核对名称、编号规则及已选 Logo。",
                        "Check the name, numbering rule and selected logo.",
                    )}
                </p>
            )}
        </section>
    );
}
