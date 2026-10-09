import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import { ApiError, checkHealth } from "./api";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import { EntityForm } from "./EntityForm";
import { AccountForm } from "./AccountForm";
import { businessError } from "./business-errors";
import { TransactionsPanel } from "./TransactionsPanel";
import { AssetsPanel } from "./AssetsPanel";
import { PeriodPanel } from "./PeriodPanel";
import type { PendingPeriodController } from "./pending-period";
import type { PendingMasterDataController } from "./pending-business";
import { MasterPanel } from "./MasterPanel";
import { BusinessDraftPanel } from "./BusinessDraftPanel";
import { RecurringPanel } from "./RecurringPanel";
import { ReminderPanel } from "./ReminderPanel";
import type { PendingReminderController } from "./pending-reminder";
import type { PendingRecurringController } from "./pending-recurring";
import type { PendingBusinessDraftController } from "./pending-business-drafts";
import { ControlBalancesPanel } from "./ControlBalancesPanel";
import { FilesPanel } from "./FilesPanel";
import { ConfirmationStatus } from "./ConfirmationStatus";
import { OcrCopyStatus } from "./OcrCopyStatus";
import type { OcrCopyController } from "./ocr-copy";
import type { PendingConfirmationController } from "./pending-confirmations";
import { OcrPanel } from "./OcrPanel";
import type { OcrJobIntents } from "./ocr-job-intents";
import type { PendingUploadController } from "./pending-upload";
import type { PendingCommandController } from "./pending-command";
import { COMPANY_CONTACT_FIELDS, REGIONS } from "./regions";
import type { CountryCode } from "./regions";
import {
    createAccount,
    createCategory,
    createEntity,
    getEntity,
    listAccounts,
    listAssets,
    listBalances,
    listCategories,
    listEntities,
    updateAccount,
    updateCategory,
    updateEntity,
} from "./ledger-api";
import type {
    Account,
    Asset,
    Balance,
    Category,
    Entity,
    EntityCreateBody,
    EntityUpdateBody,
    AccountCreateBody,
    AccountUpdateBody,
    CategoryCreateBody,
    CategoryUpdateBody,
} from "./ledger-api";
import "./workspace.css";

type View =
    | "drafts"
    | "recurring"
    | "reminders"
    | "masters"
    | "periods"
    | "controls"
    | "ocr"
    | "files"
    | "transactions"
    | "assets"
    | "overview"
    | "accounts"
    | "categories"
    | "details"
    | "settings";
const views: View[] = [
    "overview",
    "transactions",
    "files",
    "ocr",
    "accounts",
    "controls",
    "periods",
    "masters",
    "drafts",
    "recurring",
    "reminders",
    "categories",
    "details",
    "assets",
    "settings",
];
type Editor =
    | { kind: "entity"; value?: Entity }
    | { kind: "account"; value?: Account }
    | { kind: "category"; value?: Category };
type LedgerData = { id: string; accounts: Account[]; categories: Category[]; balances: Balance[] };
const text = (locale: Locale, zh: string, en: string) => (locale === "zh" ? zh : en);

function initialView(): View {
    const requested = new URLSearchParams(window.location.search).get("view");
    if (requested === "documents") return "files";
    return requested !== "files" && views.includes(requested as View)
        ? (requested as View)
        : "overview";
}

async function everyPage<T>(read: (offset: number) => Promise<T[]>): Promise<T[]> {
    const result: T[] = [];
    for (let offset = 0; offset <= 100000; offset += 100) {
        const items = await read(offset);
        result.push(...items);
        if (items.length < 100) return result;
    }
    throw new ApiError("server");
}

function CategoryForm({
    locale,
    category,
    categories,
    busy,
    error,
    onSubmit,
    onCancel,
}: {
    locale: Locale;
    category?: Category;
    categories: Category[];
    busy: boolean;
    error: string | null;
    onSubmit: (body: CategoryCreateBody | CategoryUpdateBody) => void;
    onCancel: () => void;
}) {
    const t = (zh: string, en: string) => text(locale, zh, en);
    const [id] = useState(() => crypto.randomUUID());
    const [name, setName] = useState(category?.name ?? "");
    const [english, setEnglish] = useState(category?.name_en ?? "");
    const [kind, setKind] = useState<"expense" | "income">(
        (category?.kind as "expense" | "income") ?? "expense",
    );
    const [parent, setParent] = useState(category?.parent_id ?? "");
    function submit(event: FormEvent) {
        event.preventDefault();
        if (busy) return;
        const names = { name: name.trim(), name_en: english.trim() || null };
        onSubmit(
            category
                ? { ...names, expected_version: category.version }
                : { ...names, id, kind, parent_id: parent || null },
        );
    }
    return (
        <form onSubmit={submit} aria-busy={busy}>
            <div className="form-grid">
                <div className="field">
                    <label htmlFor="category-name">{t("分类名称", "Category name")}</label>
                    <input
                        id="category-name"
                        required
                        maxLength={160}
                        value={name}
                        disabled={busy}
                        onChange={(event) => setName(event.target.value)}
                    />
                </div>
                <div className="field">
                    <label htmlFor="category-english">{t("英文名称", "English name")}</label>
                    <input
                        id="category-english"
                        maxLength={160}
                        value={english}
                        disabled={busy}
                        onChange={(event) => setEnglish(event.target.value)}
                    />
                </div>
                <div className="field">
                    <label htmlFor="category-kind">{t("类型", "Type")}</label>
                    <select
                        id="category-kind"
                        value={kind}
                        disabled={busy || !!category}
                        onChange={(event) => {
                            setKind(event.target.value as "expense" | "income");
                            setParent("");
                        }}
                    >
                        <option value="expense">{t("支出", "Expense")}</option>
                        <option value="income">{t("收入", "Income")}</option>
                    </select>
                </div>
                <div className="field">
                    <label htmlFor="category-parent">{t("上级分类", "Parent category")}</label>
                    <select
                        id="category-parent"
                        value={parent}
                        disabled={busy || !!category}
                        onChange={(event) => setParent(event.target.value)}
                    >
                        <option value="">{t("无（顶级分类）", "None (top level)")}</option>
                        {categories
                            .filter(
                                (item) =>
                                    item.kind === kind && (!item.archived || item.id === parent),
                            )
                            .map((item) => (
                                <option key={item.id} value={item.id}>
                                    {locale === "en" ? item.name_en || item.name : item.name}
                                </option>
                            ))}
                    </select>
                </div>
            </div>
            {category && (
                <p className="help-text">
                    {t(
                        "编辑时保留分类类型和层级，历史记录会继续引用此分类。",
                        "The category type and parent stay fixed, preserving historical references.",
                    )}
                </p>
            )}
            {error && (
                <p className="inline-error" role="alert">
                    {error}
                </p>
            )}
            <div className="form-actions">
                <button
                    className="secondary-button"
                    type="button"
                    onClick={onCancel}
                    disabled={busy}
                >
                    {t("取消", "Cancel")}
                </button>
                <button className="primary-button" disabled={busy}>
                    {busy ? t("保存中…", "Saving…") : t("保存", "Save")}
                </button>
            </div>
        </form>
    );
}

function SettingsPanel({ locale }: { locale: Locale }) {
    const t = (zh: string, en: string) => text(locale, zh, en);
    const [results, setResults] = useState<{ live: boolean; ready: boolean } | null>(null);
    const [loading, setLoading] = useState(true);
    const [refresh, setRefresh] = useState(0);
    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        void Promise.allSettled([
            checkHealth("live", controller.signal),
            checkHealth("ready", controller.signal),
        ]).then(([live, ready]) => {
            if (controller.signal.aborted) return;
            setResults({
                live: live.status === "fulfilled" && live.value,
                ready: ready.status === "fulfilled" && ready.value,
            });
            setLoading(false);
        });
        return () => controller.abort();
    }, [refresh]);
    return (
        <section className="business-panel">
            <div className="section-heading">
                <h2>{t("服务状态", "Service status")}</h2>
                <button
                    className="secondary-button"
                    disabled={loading}
                    onClick={() => setRefresh((value) => value + 1)}
                >
                    {t("刷新状态", "Refresh status")}
                </button>
            </div>
            <div className="health-cards">
                {(["live", "ready"] as const).map((kind) => (
                    <article className="health-card" key={kind}>
                        <h3>
                            {kind === "live"
                                ? t("应用服务", "Application service")
                                : t("数据库连接", "Database connection")}
                        </h3>
                        <span
                            data-testid={`${kind}-status`}
                            role="status"
                            className={`status-pill ${results?.[kind] ? "good" : "neutral"}`}
                        >
                            {loading
                                ? t("检查中", "Checking")
                                : results?.[kind]
                                  ? t("连接正常", "Available")
                                  : t("暂不可用", "Unavailable")}
                        </span>
                    </article>
                ))}
            </div>
            <p className="help-text">
                {t(
                    "这些检查显示当前连接状态。备份与恢复在服务器上执行。",
                    "These checks show connectivity. Backups and restores are managed on your server.",
                )}
            </p>
        </section>
    );
}

export function BusinessWorkspace({
    session,
    locale,
    commands,
    uploads,
    ocrJobs,
    confirmations,
    copies,
    periods,
    masters,
    drafts,
    recurring,
    reminders,
    onUnauthorized,
}: {
    session: Session;
    locale: Locale;
    commands: PendingCommandController;
    uploads: PendingUploadController;
    ocrJobs: OcrJobIntents;
    confirmations: PendingConfirmationController;
    copies: OcrCopyController;
    periods: PendingPeriodController;
    masters: PendingMasterDataController;
    drafts: PendingBusinessDraftController;
    recurring: PendingRecurringController;
    reminders: PendingReminderController;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => text(locale, zh, en);
    const pending = useSyncExternalStore(commands.subscribe, commands.getSnapshot);
    const confirmation = useSyncExternalStore(confirmations.subscribe, confirmations.getSnapshot);
    const confirmationLocked = !["idle", "rejected", "confirmed"].includes(confirmation.status);
    const upload = useSyncExternalStore(uploads.subscribe, uploads.getSnapshot);
    const copy = useSyncExternalStore(copies.subscribe, copies.getSnapshot);
    const copyLocked = copy.context !== null;
    const period = useSyncExternalStore(periods.subscribe, periods.getSnapshot);
    const periodLocked = !["idle", "rejected", "confirmed"].includes(period.status);
    const periodRecoveryLedger = periodLocked ? period.plan?.ledgerId : undefined;
    const draft = useSyncExternalStore(drafts.subscribe, drafts.getSnapshot);
    const draftLocked = !["idle", "rejected", "confirmed", "conflict"].includes(draft.status);
    const draftRecoveryLedger = draftLocked ? draft.plan?.ledgerId : undefined;
    const [draftEditing, setDraftEditing] = useState(false);
    const recurringState = useSyncExternalStore(recurring.subscribe, recurring.getSnapshot);
    const recurringLocked = !["idle", "rejected", "confirmed", "conflict"].includes(
        recurringState.status,
    );
    const recurringRecoveryLedger = recurringLocked ? recurringState.plan?.ledgerId : undefined;
    const [recurringEditing, setRecurringEditing] = useState(false);
    const reminderState = useSyncExternalStore(reminders.subscribe, reminders.getSnapshot);
    const reminderLocked = !["idle", "rejected", "confirmed", "conflict"].includes(
        reminderState.status,
    );
    const reminderRecoveryLedger = reminderLocked ? reminderState.plan?.ledgerId : undefined;
    const [reminderEditing, setReminderEditing] = useState(false);
    const [draftTarget, setDraftTarget] = useState<{ entityId: string; id: string } | null>(() => {
        const params = new URLSearchParams(window.location.search);
        const id = params.get("draft"),
            entityId = params.get("entity");
        return id &&
            entityId &&
            /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)
            ? { entityId, id }
            : null;
    });
    const master = useSyncExternalStore(masters.subscribe, masters.getSnapshot);
    const masterLocked = !["idle", "rejected", "confirmed", "conflict"].includes(master.status);
    const masterRecoveryLedger = masterLocked ? master.plan?.ledgerId : undefined;
    const [masterEditing, setMasterEditing] = useState(false);
    const [ocrEditing, setOcrEditing] = useState(false);
    const [fileEditing, setFileEditing] = useState(false);
    const [fileOperation, setFileOperation] = useState<string | null>(() => {
        const value = new URLSearchParams(window.location.search).get("operation");
        return value &&
            /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
            ? value
            : null;
    });
    const uploadLocked = !["idle", "rejected", "confirmed"].includes(upload.status);
    const [financialEditing, setFinancialEditing] = useState(false);
    const financialLocked = !["idle", "rejected", "confirmed"].includes(pending.status);
    const navigationLocked =
        financialEditing ||
        financialLocked ||
        fileEditing ||
        uploadLocked ||
        ocrEditing ||
        confirmationLocked ||
        copyLocked ||
        periodLocked ||
        masterLocked ||
        masterEditing ||
        draftLocked ||
        draftEditing ||
        recurringLocked ||
        recurringEditing ||
        reminderLocked ||
        reminderEditing;
    const [view, setView] = useState<View>(initialView);
    const [selectedId, setSelectedId] = useState(
        () => new URLSearchParams(window.location.search).get("entity") ?? "",
    );
    const [entities, setEntities] = useState<Entity[]>([]);
    const [assets, setAssets] = useState<Asset[]>([]);
    const [data, setData] = useState<LedgerData | null>(null);
    const [loading, setLoading] = useState(true);
    const [ledgerLoading, setLedgerLoading] = useState(false);
    const [loadError, setLoadError] = useState<unknown>(null);
    const [actionError, setActionError] = useState<unknown>(null);
    const [editor, setEditor] = useState<Editor | null>(null);
    const [busy, setBusy] = useState(false);
    const [showArchived, setShowArchived] = useState(false);
    const [refresh, setRefresh] = useState(0);
    const [notice, setNotice] = useState(false);
    const refreshGeneration = useRef(0);
    const editorTrigger = useRef<HTMLElement | null>(null);
    const onUnauthorizedRef = useRef(onUnauthorized);
    onUnauthorizedRef.current = onUnauthorized;
    const entity = entities.find((item) => item.id === selectedId);
    const ledgerId = entity?.ledger.id;
    const current = data?.id === ledgerId ? data : null;
    const visibleEntities = entities.filter(
        (item) => showArchived || !item.archived || item.id === selectedId,
    );
    const errorText = actionError ? businessError(actionError, locale) : null;
    const labels: Record<View, string> = {
        files: t("票据与证件", "Documents"),
        ocr: t("识别与复核", "Recognition and review"),
        transactions: t("流水", "Transactions"),
        assets: t("资产", "Assets"),
        overview: t("总览", "Overview"),
        accounts: t("账户", "Accounts"),
        controls: t("往来余额", "Control balances"),
        periods: t("结账与重开", "Period closing"),
        masters: t("往来与项目", "Parties and projects"),
        drafts: t("经营草稿", "Business drafts"),
        recurring: t("周期Invoice", "Recurring invoices"),
        reminders: t("提醒事项", "Reminder events"),
        categories: t("分类", "Categories"),
        details: t("账本资料", "Ledger details"),
        settings: t("设置", "Settings"),
    };

    useEffect(() => {
        if (!pending.command || !entities.length) return;
        const owner = entities.find((item) => item.ledger.id === pending.command?.ledgerId);
        if (owner) {
            setSelectedId(owner.id);
            setView("transactions");
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps -- Navigate only when the frozen command key or entity list changes; retries retain its ledger.
    }, [pending.command?.key, entities]);
    useEffect(() => {
        if (
            !financialLocked &&
            !uploadLocked &&
            !confirmationLocked &&
            !copyLocked &&
            !periodLocked &&
            !masterLocked &&
            !draftLocked &&
            !recurringLocked &&
            !reminderLocked
        )
            return;
        const warn = (event: BeforeUnloadEvent) => {
            event.preventDefault();
            event.returnValue = "";
        };
        window.addEventListener("beforeunload", warn);
        return () => window.removeEventListener("beforeunload", warn);
    }, [
        financialLocked,
        uploadLocked,
        confirmationLocked,
        copyLocked,
        periodLocked,
        masterLocked,
        draftLocked,
        recurringLocked,
        reminderLocked,
    ]);
    useEffect(() => {
        if (!upload.command || !entities.length) return;
        const owner = entities.find((item) => item.ledger.id === upload.command?.ledgerId);
        if (owner) {
            setSelectedId(owner.id);
            setFileOperation(upload.command.operationId);
            setView("files");
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps -- Navigate only when the frozen upload ID or entity list changes; retries retain its ledger and operation.
    }, [upload.command?.uploadId, entities]);

    useEffect(() => {
        if (!recurringRecoveryLedger) return;
        const owner = entities.find((item) => item.ledger.id === recurringRecoveryLedger);
        if (owner) {
            setSelectedId(owner.id);
            setView("recurring");
        }
    }, [recurringRecoveryLedger, entities]);
    useEffect(() => {
        if (!reminderRecoveryLedger) return;
        const owner = entities.find((item) => item.ledger.id === reminderRecoveryLedger);
        if (owner) {
            setSelectedId(owner.id);
            setView("reminders");
        }
    }, [reminderRecoveryLedger, entities]);
    useEffect(() => {
        if (!draftRecoveryLedger) return;
        const owner = entities.find((item) => item.ledger.id === draftRecoveryLedger);
        if (owner) {
            setSelectedId(owner.id);
            setView("drafts");
        }
    }, [draftRecoveryLedger, entities]);
    useEffect(() => {
        if (!masterRecoveryLedger) return;
        const owner = entities.find((item) => item.ledger.id === masterRecoveryLedger);
        if (owner) {
            setSelectedId(owner.id);
            setView("masters");
        }
    }, [masterRecoveryLedger, entities]);
    useEffect(() => {
        if (!periodRecoveryLedger) return;
        const owner = entities.find((item) => item.ledger.id === periodRecoveryLedger);
        if (owner) {
            setSelectedId(owner.id);
            setView("periods");
        }
    }, [periodRecoveryLedger, entities]);

    function failure(error: unknown) {
        if (error instanceof ApiError && error.kind === "unauthorized") onUnauthorizedRef.current();
        else setActionError(error);
    }
    useEffect(() => {
        const controller = new AbortController();
        setLoading(true);
        setLoadError(null);
        void Promise.all([
            everyPage((offset) =>
                listEntities({ include_archived: true, limit: 100, offset }, controller.signal),
            ),
            everyPage((offset) =>
                listAssets({ include_disabled: true, limit: 100, offset }, controller.signal),
            ),
        ])
            .then(([loadedEntities, loadedAssets]) => {
                if (controller.signal.aborted) return;
                setEntities(loadedEntities);
                setAssets(loadedAssets);
                setSelectedId((previous) =>
                    loadedEntities.some((item) => item.id === previous)
                        ? previous
                        : ((loadedEntities.find((item) => !item.archived) ?? loadedEntities[0])
                              ?.id ?? ""),
                );
                setLoading(false);
            })
            .catch((error) => {
                if (controller.signal.aborted) return;
                setLoading(false);
                if (error instanceof ApiError && error.kind === "unauthorized")
                    onUnauthorizedRef.current();
                else setLoadError(error);
            });
        return () => controller.abort();
    }, [session.user.id, refresh]);

    useEffect(() => {
        if (!ledgerId) {
            setData(null);
            return;
        }
        const controller = new AbortController();
        setLedgerLoading(true);
        setLoadError(null);
        void Promise.all([
            everyPage((offset) =>
                listAccounts(
                    ledgerId,
                    { include_archived: true, limit: 100, offset },
                    controller.signal,
                ),
            ),
            everyPage((offset) =>
                listCategories(
                    ledgerId,
                    { include_archived: true, limit: 100, offset },
                    controller.signal,
                ),
            ),
            everyPage((offset) =>
                listBalances(ledgerId, { limit: 100, offset }, controller.signal),
            ),
        ])
            .then(([accounts, categories, balances]) => {
                if (controller.signal.aborted) return;
                setData({ id: ledgerId, accounts, categories, balances });
                setLedgerLoading(false);
            })
            .catch((error) => {
                if (controller.signal.aborted) return;
                setData(null);
                setLedgerLoading(false);
                if (error instanceof ApiError && error.kind === "unauthorized")
                    onUnauthorizedRef.current();
                else setLoadError(error);
            });
        return () => controller.abort();
    }, [ledgerId, refresh, session.user.id]);

    useEffect(() => {
        const params = new URLSearchParams();
        // Keep the established public URL while using the files name internally.
        params.set("view", view === "files" ? "documents" : view);
        if (view === "files" && fileOperation) params.set("operation", fileOperation);
        if (selectedId) params.set("entity", selectedId);
        if (view === "drafts" && draftTarget?.entityId === selectedId)
            params.set("draft", draftTarget.id);
        window.history.replaceState(null, "", `/?${params.toString()}`);
        document.title = `${labels[view]} · Coinpup`;
        // eslint-disable-next-line react-hooks/exhaustive-deps -- Labels are derived solely from locale; view and locale already trigger the title update.
    }, [view, selectedId, locale, fileOperation, draftTarget]);

    function openEditor(next: Editor) {
        editorTrigger.current =
            document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setActionError(null);
        setNotice(false);
        setEditor(next);
    }
    function closeEditor() {
        const trigger = editorTrigger.current;
        setEditor(null);
        setActionError(null);
        requestAnimationFrame(() => {
            if (trigger?.isConnected) trigger.focus();
            else document.getElementById("business-title")?.focus();
        });
    }
    function reload() {
        setLoadError(null);
        setActionError(null);
        setRefresh((value) => value + 1);
    }
    async function save(
        body:
            | EntityCreateBody
            | EntityUpdateBody
            | AccountCreateBody
            | AccountUpdateBody
            | CategoryCreateBody
            | CategoryUpdateBody,
    ) {
        if (!editor || busy) return;
        setBusy(true);
        setActionError(null);
        setNotice(false);
        const generation = ++refreshGeneration.current;
        try {
            const csrf = session.csrf_token;
            if (editor.kind === "entity") {
                const saved = editor.value
                    ? await updateEntity(csrf, editor.value.id, body as EntityUpdateBody)
                    : await createEntity(csrf, body as EntityCreateBody);
                setEntities((previous) => [
                    ...previous.filter((item) => item.id !== saved.id),
                    saved,
                ]);
                setSelectedId(saved.id);
            } else if (ledgerId && editor.kind === "account") {
                if (editor.value)
                    await updateAccount(csrf, ledgerId, editor.value.id, body as AccountUpdateBody);
                else await createAccount(csrf, ledgerId, body as AccountCreateBody);
            } else if (ledgerId && editor.kind === "category") {
                if (editor.value)
                    await updateCategory(
                        csrf,
                        ledgerId,
                        editor.value.id,
                        body as CategoryUpdateBody,
                    );
                else await createCategory(csrf, ledgerId, body as CategoryCreateBody);
            } else throw new ApiError("server");
            if (generation !== refreshGeneration.current) return;
            setEditor(null);
            setNotice(true);
            setRefresh((value) => value + 1);
        } catch (error) {
            failure(error);
        } finally {
            setBusy(false);
        }
    }

    async function reloadEditor() {
        if (!editor?.value || busy) return;
        setBusy(true);
        setActionError(null);
        try {
            if (editor.kind === "entity") {
                const value = await getEntity(editor.value.id);
                setEditor({ kind: "entity", value });
                setEntities((previous) =>
                    previous.map((item) => (item.id === value.id ? value : item)),
                );
            } else if (ledgerId && editor.kind === "account") {
                const accounts = await everyPage((offset) =>
                    listAccounts(ledgerId, { include_archived: true, offset, limit: 100 }),
                );
                const value = accounts.find((item) => item.id === editor.value?.id);
                if (!value) throw new ApiError("server");
                setEditor({ kind: "account", value });
            } else if (ledgerId && editor.kind === "category") {
                const categories = await everyPage((offset) =>
                    listCategories(ledgerId, { include_archived: true, offset, limit: 100 }),
                );
                const value = categories.find((item) => item.id === editor.value?.id);
                if (!value) throw new ApiError("server");
                setEditor({ kind: "category", value });
            }
        } catch (error) {
            failure(error);
        } finally {
            setBusy(false);
        }
    }

    async function archive(
        kind: "entity" | "account" | "category",
        record: Entity | Account | Category,
    ) {
        if (busy) return;
        setBusy(true);
        setActionError(null);
        setNotice(false);
        const change = { expected_version: record.version, archived: !record.archived };
        try {
            if (kind === "entity") await updateEntity(session.csrf_token, record.id, change);
            else if (ledgerId && kind === "account")
                await updateAccount(session.csrf_token, ledgerId, record.id, change);
            else if (ledgerId)
                await updateCategory(session.csrf_token, ledgerId, record.id, change);
            setNotice(true);
            setRefresh((value) => value + 1);
        } catch (error) {
            failure(error);
        } finally {
            setBusy(false);
        }
    }

    const accounts = current?.accounts.filter((item) => showArchived || !item.archived) ?? [];
    const categories = current?.categories.filter((item) => showArchived || !item.archived) ?? [];
    const editorTitle =
        editor?.kind === "entity"
            ? editor.value
                ? t("编辑资料", "Edit details")
                : t("新增账本", "New ledger")
            : editor?.kind === "account"
              ? editor.value
                  ? t("编辑账户", "Edit account")
                  : t("新增账户", "New account")
              : editor?.value
                ? t("编辑分类", "Edit category")
                : t("新增分类", "New category");
    const editorKey = editor
        ? `${editor.kind}:${editor.value?.id ?? "new"}:${editor.value?.version ?? 0}`
        : "";
    useEffect(() => {
        if (editorKey) document.getElementById("business-title")?.focus();
    }, [editorKey]);

    return (
        <div className="business-layout">
            <aside className="business-sidebar" aria-label={t("管理空间", "Workspace")}>
                <div className="business-profile">
                    <span className="avatar">{session.user.username.charAt(0).toUpperCase()}</span>
                    <div>
                        <strong data-testid="current-username">{session.user.username}</strong>
                        <small>{t("私人财务空间", "Private financial space")}</small>
                    </div>
                </div>
                <div className="ledger-picker field">
                    <label htmlFor="active-ledger">{t("账本", "Ledger")}</label>
                    <select
                        id="active-ledger"
                        value={selectedId}
                        disabled={loading || busy || !!editor || navigationLocked}
                        onChange={(event) => {
                            setSelectedId(event.target.value);
                            setDraftTarget(null);
                            setFileOperation(null);
                            setActionError(null);
                            setNotice(false);
                        }}
                    >
                        <option value="" disabled>
                            {t("选择账本", "Choose a ledger")}
                        </option>
                        {visibleEntities.map((item) => (
                            <option key={item.id} value={item.id}>
                                {item.name}
                                {item.archived ? t("（已归档）", " (archived)") : ""}
                            </option>
                        ))}
                    </select>
                </div>
                <button
                    className="new-ledger-button"
                    disabled={busy || !!editor || loading || navigationLocked}
                    onClick={() => openEditor({ kind: "entity" })}
                >
                    <span aria-hidden="true">＋ </span>
                    {t("新增账本", "New ledger")}
                </button>
                <nav
                    className="business-navigation"
                    aria-label={t("工作区导航", "Workspace navigation")}
                >
                    {views.map((item) => (
                        <button
                            key={item}
                            disabled={busy || !!editor || navigationLocked}
                            aria-current={view === item ? "page" : undefined}
                            onClick={() => {
                                setView(item);
                                setDraftTarget(null);
                                setFileOperation(null);
                                setActionError(null);
                                setNotice(false);
                            }}
                        >
                            <span aria-hidden="true">
                                {
                                    {
                                        files: "▧",
                                        ocr: "▦",
                                        transactions: "⇄",
                                        assets: "◈",
                                        overview: "◫",
                                        accounts: "▣",
                                        controls: "⇆",
                                        periods: "▣",
                                        masters: "◇",
                                        drafts: "▤",
                                        recurring: "↻",
                                        reminders: "◷",
                                        categories: "⊞",
                                        details: "▤",
                                        settings: "⚙",
                                    }[item]
                                }
                            </span>
                            {labels[item]}
                        </button>
                    ))}
                </nav>
                <p className="sidebar-caption">
                    {t("每个账本，独立记录。", "Each ledger keeps its own records.")}
                </p>
            </aside>
            <main id="main" className="business-main">
                <OcrCopyStatus
                    copies={copies}
                    canResume={entities.some((item) => item.ledger.id === copy.context?.ledger)}
                    session={session}
                    locale={locale}
                    onUnauthorized={onUnauthorized}
                    onResume={() => {
                        const source = entities.find(
                            (item) => item.ledger.id === copy.context?.ledger,
                        );
                        if (source) {
                            setSelectedId(source.id);
                            setView("ocr");
                        }
                    }}
                />
                <ConfirmationStatus
                    controller={confirmations}
                    session={session}
                    locale={locale}
                    onUnauthorized={onUnauthorized}
                />
                <header className="business-heading">
                    <div>
                        <p className="eyebrow">
                            {entity
                                ? entity.kind === "company"
                                    ? t("公司账本", "Company ledger")
                                    : t("个人账本", "Personal ledger")
                                : "COINPUP"}
                        </p>
                        <h1 id="business-title" tabIndex={-1}>
                            {editor ? editorTitle : labels[view]}
                        </h1>
                        <p className="muted">
                            {entity?.name ??
                                t("从你的第一个账本开始。", "Start with your first ledger.")}
                        </p>
                    </div>
                    {!editor && !navigationLocked && (
                        <button
                            className="secondary-button"
                            disabled={loading || ledgerLoading || busy}
                            onClick={reload}
                        >
                            {t("刷新", "Refresh")}
                        </button>
                    )}
                </header>
                {loadError !== null && (
                    <div className="inline-error" role="alert">
                        <p>{businessError(loadError, locale)}</p>
                        <button
                            type="button"
                            disabled={loading || ledgerLoading || busy}
                            onClick={reload}
                        >
                            {t("重试读取", "Retry loading")}
                        </button>
                    </div>
                )}
                {!editor && errorText && (
                    <p className="inline-error" role="alert">
                        {errorText}
                    </p>
                )}
                {notice && !editor && (
                    <p className="save-notice" role="status">
                        {t("已保存。", "Saved.")}
                    </p>
                )}
                {editor ? (
                    <section className="business-panel form-panel">
                        {editor.kind === "entity" && (
                            <EntityForm
                                key={editorKey}
                                locale={locale}
                                assets={assets}
                                entity={editor.value}
                                busy={busy}
                                error={errorText}
                                onSubmit={(body) => void save(body)}
                                onCancel={closeEditor}
                            />
                        )}
                        {editor.kind === "account" && (
                            <AccountForm
                                key={editorKey}
                                locale={locale}
                                assets={assets}
                                account={editor.value}
                                busy={busy}
                                error={errorText}
                                onSubmit={(body) => void save(body)}
                                onCancel={closeEditor}
                            />
                        )}
                        {editor.kind === "category" && (
                            <CategoryForm
                                key={editorKey}
                                locale={locale}
                                category={editor.value}
                                categories={current?.categories ?? []}
                                busy={busy}
                                error={errorText}
                                onSubmit={(body) => void save(body)}
                                onCancel={closeEditor}
                            />
                        )}
                        {editor.value &&
                            actionError instanceof ApiError &&
                            actionError.status === 409 && (
                                <button
                                    className="text-button reload-editor"
                                    disabled={busy}
                                    onClick={() => void reloadEditor()}
                                >
                                    {t("重新载入", "Reload")}
                                </button>
                            )}
                    </section>
                ) : view === "settings" ? (
                    <SettingsPanel locale={locale} />
                ) : view === "assets" ? (
                    <AssetsPanel
                        session={session}
                        locale={locale}
                        assets={assets}
                        loading={loading}
                        onChanged={reload}
                        onUnauthorized={onUnauthorized}
                        onEditingChange={setFinancialEditing}
                    />
                ) : loading && !entities.length ? (
                    <div className="business-panel" role="status">
                        {t("正在读取账本…", "Loading ledgers…")}
                    </div>
                ) : !entity ? (
                    <section className="empty-state business-panel">
                        <span className="empty-symbol" aria-hidden="true">
                            ◫
                        </span>
                        <h2>{t("生活与事业，各有一本账。", "A place for life and business.")}</h2>
                        <p>
                            {t(
                                "新建个人或公司账本，选择分类模板，再添加你的付款账户。",
                                "Create a personal or company ledger, choose a category template and add your accounts.",
                            )}
                        </p>
                        <button
                            className="primary-button"
                            disabled={loading || busy || navigationLocked}
                            onClick={() => openEditor({ kind: "entity" })}
                        >
                            {t("创建第一个账本", "Create your first ledger")}
                        </button>
                    </section>
                ) : (
                    <>
                        {entity.archived && (
                            <p className="archive-banner">
                                {t(
                                    "此账本已归档，历史和余额仍可查看。恢复账本后可继续编辑。",
                                    "This ledger is archived. History and balances remain available; restore it to edit.",
                                )}
                            </p>
                        )}
                        {view !== "transactions" &&
                            view !== "files" &&
                            view !== "ocr" &&
                            view !== "recurring" &&
                            view !== "reminders" && (
                                <label className="archive-toggle">
                                    <input
                                        type="checkbox"
                                        checked={showArchived}
                                        onChange={(event) => setShowArchived(event.target.checked)}
                                    />
                                    {t("显示已归档", "Show archived")}
                                </label>
                            )}
                        {ledgerLoading && (
                            <p className="help-text" role="status">
                                {t("正在读取此账本…", "Loading this ledger…")}
                            </p>
                        )}
                        {view === "reminders" && (
                            <ReminderPanel
                                key={`${session.user.id}:${entity.ledger.id}`}
                                ledgerId={entity.ledger.id}
                                locale={locale}
                                session={session}
                                controller={reminders}
                                archived={entity.archived}
                                dataLoading={loading || ledgerLoading || !current}
                                onUnauthorized={onUnauthorized}
                                onEditing={setReminderEditing}
                            />
                        )}
                        {view === "recurring" && (
                            <RecurringPanel
                                key={entity.ledger.id}
                                ledgerId={entity.ledger.id}
                                locale={locale}
                                session={session}
                                controller={recurring}
                                archived={entity.archived}
                                dataLoading={loading || ledgerLoading || !current}
                                onUnauthorized={onUnauthorized}
                                onEditing={setRecurringEditing}
                                onOpenDraft={(id) => {
                                    setDraftTarget({ entityId: entity.id, id });
                                    setView("drafts");
                                }}
                            />
                        )}
                        {view === "drafts" && (
                            <BusinessDraftPanel
                                key={entity.ledger.id}
                                ledgerId={entity.ledger.id}
                                locale={locale}
                                session={session}
                                controller={drafts}
                                initialId={
                                    draftTarget?.entityId === entity.id ? draftTarget.id : undefined
                                }
                                onFocusFinished={() => setDraftTarget(null)}
                                assets={assets}
                                categories={current?.categories ?? []}
                                archived={entity.archived}
                                dataLoading={loading || ledgerLoading || !current}
                                onUnauthorized={onUnauthorized}
                                onEditing={setDraftEditing}
                            />
                        )}
                        {view === "masters" && (
                            <MasterPanel
                                key={entity.ledger.id}
                                ledgerId={entity.ledger.id}
                                locale={locale}
                                session={session}
                                controller={masters}
                                archived={entity.archived}
                                onUnauthorized={onUnauthorized}
                                onEditing={setMasterEditing}
                            />
                        )}
                        {view === "periods" && (
                            <PeriodPanel
                                key={entity.ledger.id}
                                ledgerId={entity.ledger.id}
                                locale={locale}
                                session={session}
                                controller={periods}
                                onUnauthorized={onUnauthorized}
                            />
                        )}
                        {view === "controls" && (
                            <ControlBalancesPanel
                                key={entity.ledger.id}
                                ledgerId={entity.ledger.id}
                                locale={locale}
                                assets={assets}
                                onUnauthorized={onUnauthorized}
                            />
                        )}
                        {view === "transactions" && (
                            <TransactionsPanel
                                key={entity.ledger.id}
                                session={session}
                                locale={locale}
                                entity={entity}
                                accounts={current?.accounts ?? []}
                                assets={assets}
                                categories={current?.categories ?? []}
                                controller={commands}
                                onFiles={(operationId) => {
                                    setFileOperation(operationId);
                                    setView("files");
                                }}
                                dataLoading={loading || ledgerLoading || !current}
                                onChanged={reload}
                                onUnauthorized={onUnauthorized}
                                onEditingChange={setFinancialEditing}
                            />
                        )}
                        {view === "ocr" && (
                            <OcrPanel
                                key={entity.ledger.id}
                                session={session}
                                locale={locale}
                                entity={entity}
                                jobs={ocrJobs}
                                controller={confirmations}
                                copies={copies}
                                entities={entities}
                                accounts={current?.accounts ?? []}
                                assets={assets}
                                categories={current?.categories ?? []}
                                dataLoading={loading || ledgerLoading || !current}
                                onUnauthorized={onUnauthorized}
                                onEditingChange={setOcrEditing}
                            />
                        )}
                        {view === "files" && (
                            <FilesPanel
                                key={`${entity.ledger.id}:${fileOperation ?? "all"}`}
                                session={session}
                                locale={locale}
                                entity={entity}
                                operationId={fileOperation}
                                controller={uploads}
                                onUnauthorized={onUnauthorized}
                                onEditingChange={setFileEditing}
                                onClose={
                                    fileOperation
                                        ? () => {
                                              const target = `files-button-${fileOperation}`;
                                              setFileOperation(null);
                                              setView("transactions");
                                              requestAnimationFrame(() =>
                                                  (
                                                      document.getElementById(target) ??
                                                      document.getElementById("business-title")
                                                  )?.focus(),
                                              );
                                          }
                                        : undefined
                                }
                            />
                        )}
                        {view === "overview" && (
                            <>
                                <div className="overview-cards">
                                    <article className="summary-card">
                                        <p>{t("账户", "Accounts")}</p>
                                        <strong>
                                            {current?.accounts.filter((item) => !item.archived)
                                                .length ?? "—"}
                                        </strong>
                                        <span>
                                            {t(
                                                "支持多种货币与资产",
                                                "Multiple currencies and assets",
                                            )}
                                        </span>
                                    </article>
                                    <article className="summary-card">
                                        <p>{t("独立分类", "Independent categories")}</p>
                                        <strong>
                                            {current?.categories.filter((item) => !item.archived)
                                                .length ?? "—"}
                                        </strong>
                                        <span>
                                            {t(
                                                "收入与支出分别管理",
                                                "Separate income and expenses",
                                            )}
                                        </span>
                                    </article>
                                    <article className="summary-card">
                                        <p>{t("基础币种", "Base currency")}</p>
                                        <strong>{entity.ledger.base_asset_id}</strong>
                                        <span>
                                            {t(
                                                "原币金额始终保留",
                                                "Original quantities are retained",
                                            )}
                                        </span>
                                    </article>
                                </div>
                                <section className="business-panel">
                                    <div className="section-heading">
                                        <div>
                                            <h2>{t("账户余额", "Account balances")}</h2>
                                            <p className="help-text">
                                                {t(
                                                    "按账户与原币分别显示。",
                                                    "Shown separately by account and original asset.",
                                                )}
                                            </p>
                                        </div>
                                        <button
                                            className="secondary-button"
                                            disabled={
                                                entity.archived || busy || ledgerLoading || loading
                                            }
                                            onClick={() => openEditor({ kind: "account" })}
                                        >
                                            {t("新增账户", "New account")}
                                        </button>
                                    </div>
                                    {!current?.accounts.length ? (
                                        <p className="empty-copy">
                                            {t(
                                                "还没有账户。添加银行、现金或付款平台账户。",
                                                "No accounts yet. Add a bank, cash or payment platform account.",
                                            )}
                                        </p>
                                    ) : (
                                        <div className="balance-list">
                                            {current.balances
                                                .filter(
                                                    (balance) =>
                                                        showArchived || !balance.account_archived,
                                                )
                                                .map((balance) => (
                                                    <div
                                                        className="balance-row"
                                                        key={`${balance.account_id}:${balance.asset_id}`}
                                                    >
                                                        <span>
                                                            {current.accounts.find(
                                                                (account) =>
                                                                    account.id ===
                                                                    balance.account_id,
                                                            )?.name ?? "—"}
                                                        </span>
                                                        <span className="asset-code">
                                                            {balance.asset_id}
                                                        </span>
                                                        <strong
                                                            className="money-quantity"
                                                            tabIndex={0}
                                                            aria-label={`${balance.asset_id} ${balance.amount}`}
                                                        >
                                                            {balance.amount}
                                                        </strong>
                                                        {(!balance.asset_enabled ||
                                                            !balance.link_enabled) && (
                                                            <small>{t("已停用", "Disabled")}</small>
                                                        )}
                                                    </div>
                                                ))}
                                        </div>
                                    )}
                                </section>
                            </>
                        )}
                        {view === "accounts" && (
                            <section className="business-panel">
                                <div className="section-heading">
                                    <h2>{t("付款与收款账户", "Payment accounts")}</h2>
                                    <button
                                        className="primary-button"
                                        disabled={
                                            entity.archived || busy || ledgerLoading || loading
                                        }
                                        onClick={() => openEditor({ kind: "account" })}
                                    >
                                        {t("新增账户", "New account")}
                                    </button>
                                </div>
                                <div className="account-grid">
                                    {accounts.map((account) => (
                                        <article
                                            className="account-card"
                                            data-testid={`account-${account.id}`}
                                            key={account.id}
                                        >
                                            <div className="record-heading">
                                                <h3>{account.name}</h3>
                                                {account.archived && (
                                                    <span className="record-badge">
                                                        {t("已归档", "Archived")}
                                                    </span>
                                                )}
                                            </div>
                                            <p className="help-text">
                                                {account.kind === "bank"
                                                    ? t("银行账户", "Bank account")
                                                    : account.kind === "cash"
                                                      ? t("现金", "Cash")
                                                      : account.kind === "credit_card"
                                                        ? t("信用卡", "Credit card")
                                                        : account.kind === "crypto"
                                                          ? t("加密资产", "Crypto assets")
                                                          : ({
                                                                wechat: t("微信", "WeChat"),
                                                                alipay: t("支付宝", "Alipay"),
                                                                paypal: "PayPal",
                                                                wise: "Wise",
                                                                stripe: "Stripe",
                                                            }[account.kind] ?? account.kind)}
                                            </p>
                                            <dl className="account-balances">
                                                {current?.balances
                                                    .filter(
                                                        (item) => item.account_id === account.id,
                                                    )
                                                    .map((balance) => (
                                                        <div key={balance.asset_id}>
                                                            <dt>
                                                                {balance.asset_id}
                                                                {(!balance.link_enabled ||
                                                                    !balance.asset_enabled) && (
                                                                    <small>
                                                                        {t(
                                                                            " · 已停用",
                                                                            " · Disabled",
                                                                        )}
                                                                    </small>
                                                                )}
                                                            </dt>
                                                            <dd
                                                                tabIndex={0}
                                                                aria-label={`${balance.asset_id} ${balance.amount}`}
                                                            >
                                                                {balance.amount}
                                                            </dd>
                                                        </div>
                                                    ))}
                                            </dl>
                                            <div className="row-actions">
                                                <button
                                                    disabled={
                                                        entity.archived ||
                                                        busy ||
                                                        ledgerLoading ||
                                                        loading
                                                    }
                                                    onClick={() =>
                                                        openEditor({
                                                            kind: "account",
                                                            value: account,
                                                        })
                                                    }
                                                >
                                                    {t("编辑", "Edit")}
                                                </button>
                                                <button
                                                    disabled={
                                                        entity.archived ||
                                                        busy ||
                                                        ledgerLoading ||
                                                        loading
                                                    }
                                                    onClick={() => void archive("account", account)}
                                                >
                                                    {account.archived
                                                        ? t("恢复", "Restore")
                                                        : t("归档", "Archive")}
                                                </button>
                                            </div>
                                        </article>
                                    ))}
                                </div>
                                {!accounts.length && (
                                    <p className="empty-copy">
                                        {t(
                                            "这个账本还没有可显示的账户。",
                                            "There are no accounts to show in this ledger.",
                                        )}
                                    </p>
                                )}
                            </section>
                        )}
                        {view === "categories" && (
                            <section className="business-panel">
                                <div className="section-heading">
                                    <h2>{t("分类管理", "Manage categories")}</h2>
                                    <button
                                        className="primary-button"
                                        disabled={
                                            entity.archived || busy || ledgerLoading || loading
                                        }
                                        onClick={() => openEditor({ kind: "category" })}
                                    >
                                        {t("新增分类", "New category")}
                                    </button>
                                </div>
                                <p className="help-text">
                                    {t(
                                        "分类只属于当前账本。归档会保留已有记录的引用。",
                                        "Categories belong to this ledger. Archiving preserves historical references.",
                                    )}
                                </p>
                                <div className="category-list">
                                    {categories.map((category) => (
                                        <article
                                            className="category-row"
                                            key={category.id}
                                            data-testid={`category-${category.id}`}
                                        >
                                            <div
                                                className={`category-marker ${category.kind}`}
                                                aria-hidden="true"
                                            >
                                                {category.kind === "income" ? "+" : "−"}
                                            </div>
                                            <div className="category-title">
                                                <h3>
                                                    {locale === "en"
                                                        ? category.name_en || category.name
                                                        : category.name}
                                                </h3>
                                                <p>
                                                    {category.kind === "income"
                                                        ? t("收入", "Income")
                                                        : t("支出", "Expense")}
                                                    {category.parent_id && (
                                                        <>
                                                            {" "}
                                                            ·{" "}
                                                            {
                                                                current?.categories.find(
                                                                    (item) =>
                                                                        item.id ===
                                                                        category.parent_id,
                                                                )?.name
                                                            }
                                                        </>
                                                    )}
                                                    {category.archived && (
                                                        <> · {t("已归档", "Archived")}</>
                                                    )}
                                                </p>
                                            </div>
                                            <div className="row-actions">
                                                <button
                                                    disabled={
                                                        entity.archived ||
                                                        busy ||
                                                        ledgerLoading ||
                                                        loading
                                                    }
                                                    onClick={() =>
                                                        openEditor({
                                                            kind: "category",
                                                            value: category,
                                                        })
                                                    }
                                                >
                                                    {t("编辑", "Edit")}
                                                </button>
                                                <button
                                                    disabled={
                                                        entity.archived ||
                                                        busy ||
                                                        ledgerLoading ||
                                                        loading
                                                    }
                                                    onClick={() =>
                                                        void archive("category", category)
                                                    }
                                                >
                                                    {category.archived
                                                        ? t("恢复", "Restore")
                                                        : t("归档", "Archive")}
                                                </button>
                                            </div>
                                        </article>
                                    ))}
                                </div>
                                {!categories.length && (
                                    <p className="empty-copy">
                                        {t(
                                            "还没有分类。你可以添加收入或支出分类。",
                                            "No categories yet. Add an income or expense category.",
                                        )}
                                    </p>
                                )}
                            </section>
                        )}
                        {view === "details" && (
                            <section className="business-panel">
                                <div className="section-heading">
                                    <h2>{entity.name}</h2>
                                    <button
                                        className="primary-button"
                                        disabled={busy || entity.archived || loading}
                                        onClick={() =>
                                            openEditor({ kind: "entity", value: entity })
                                        }
                                    >
                                        {t("编辑资料", "Edit details")}
                                    </button>
                                </div>
                                <dl className="profile-details">
                                    <div>
                                        <dt>{t("账本类型", "Ledger type")}</dt>
                                        <dd>
                                            {entity.kind === "company"
                                                ? t("公司", "Company")
                                                : t("个人", "Personal")}
                                        </dd>
                                    </div>
                                    {entity.legal_name && (
                                        <div>
                                            <dt>{t("法定名称", "Legal name")}</dt>
                                            <dd>{entity.legal_name}</dd>
                                        </div>
                                    )}
                                    {entity.country_code && (
                                        <div>
                                            <dt>{t("注册地区", "Jurisdiction")}</dt>
                                            <dd>
                                                {REGIONS[entity.country_code as CountryCode]?.name[
                                                    locale
                                                ] ?? entity.country_code}
                                                {entity.region_code
                                                    ? ` · ${entity.region_code}`
                                                    : ""}
                                            </dd>
                                        </div>
                                    )}
                                    {entity.registration_date && (
                                        <div>
                                            <dt>{t("登记日期", "Registration date")}</dt>
                                            <dd>{entity.registration_date}</dd>
                                        </div>
                                    )}
                                    <div>
                                        <dt>{t("基础币种", "Base currency")}</dt>
                                        <dd>{entity.ledger.base_asset_id}</dd>
                                    </div>
                                    {Object.entries(entity.details).map(([key, value]) => (
                                        <div key={key}>
                                            <dt>
                                                {[
                                                    ...(entity.country_code
                                                        ? (REGIONS[
                                                              entity.country_code as CountryCode
                                                          ]?.fields ?? [])
                                                        : []),
                                                    ...COMPANY_CONTACT_FIELDS,
                                                ].find((field) => field.key === key)?.label[
                                                    locale
                                                ] ?? key}
                                            </dt>
                                            <dd>{value}</dd>
                                        </div>
                                    ))}
                                </dl>
                                <div className="form-actions">
                                    <button
                                        className="secondary-button"
                                        disabled={busy || loading}
                                        onClick={() => {
                                            setFileOperation(null);
                                            setView("files");
                                        }}
                                    >
                                        {entity.kind === "company"
                                            ? t("公司证件与票据", "Company documents")
                                            : t("个人票据", "Personal documents")}
                                    </button>
                                </div>
                                <div className="archive-section">
                                    <p className="help-text">
                                        {t(
                                            "归档保留资料、余额及历史，并暂停此账本的新操作。",
                                            "Archiving retains details, balances and history, and pauses new ledger activity.",
                                        )}
                                    </p>
                                    <button
                                        className="secondary-button"
                                        disabled={busy || loading}
                                        onClick={() => void archive("entity", entity)}
                                    >
                                        {entity.archived
                                            ? t("恢复账本", "Restore ledger")
                                            : t("归档账本", "Archive ledger")}
                                    </button>
                                </div>
                            </section>
                        )}
                    </>
                )}
                <footer className="business-footer">
                    Coinpup · {t("原币记录，清楚归属。", "Original quantities. Clear ownership.")}
                </footer>
            </main>
        </div>
    );
}
