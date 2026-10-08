import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ComponentProps } from "react";
import { ApiError } from "./api";
import { listAccounts, listCategories } from "./ledger-api";
import type { Account, Category, Entity } from "./ledger-api";
import { OcrEntryForm } from "./OcrEntryForm";
import { destinationEntry, savedDestination } from "./ocr-entry";
import { saveDraftReview } from "./ocr-api";
import type { OcrCopyController } from "./ocr-copy";

async function all<T>(read: (offset: number) => Promise<T[]>) {
    const rows: T[] = [];
    for (let offset = 0; offset <= 100000; offset += 100) {
        const items = await read(offset);
        rows.push(...items);
        if (items.length < 100) return rows;
    }
    throw new ApiError("server", null, "pagination_limit");
}
export function OcrDestination(
    props: ComponentProps<typeof OcrEntryForm> & {
        entities: Entity[];
        copies: OcrCopyController;
    },
) {
    const { ledger, file, review, copies, locale, onEditing, onError } = props;
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const target = savedDestination(review.review.entry) ?? { ledger, file };
    const [choice, setChoice] = useState(target.ledger),
        [busy, setBusy] = useState(false);
    const [childEditing, setChildEditing] = useState(false);
    const [refRefresh, setRefRefresh] = useState(0),
        [refBusy, setRefBusy] = useState(false);
    const [references, setReferences] = useState<{
        ledger: string;
        accounts: Account[];
        categories: Category[];
    } | null>(null);
    const copy = useSyncExternalStore(copies.subscribe, copies.getSnapshot);
    const failed = useRef(onError);
    useEffect(() => {
        failed.current = onError;
    }, [onError]);
    useEffect(() => {
        setChoice(target.ledger);
    }, [target.ledger, review.draft_id]);
    useEffect(() => {
        onEditing(childEditing || busy);
        return () => onEditing(false);
    }, [childEditing, busy, onEditing]);
    useEffect(() => {
        setReferences(null);
        if (target.ledger === ledger) return;
        const abort = new AbortController();
        setRefBusy(true);
        void Promise.all([
            all((offset) =>
                listAccounts(
                    target.ledger,
                    { limit: 100, offset, include_archived: true },
                    abort.signal,
                ),
            ),
            all((offset) =>
                listCategories(
                    target.ledger,
                    { limit: 100, offset, include_archived: true },
                    abort.signal,
                ),
            ),
        ])
            .then(([accounts, categories]) => {
                if (!abort.signal.aborted)
                    setReferences({ ledger: target.ledger, accounts, categories });
            })
            .catch((error) => {
                if (!abort.signal.aborted) failed.current(error);
            })
            .finally(() => {
                if (!abort.signal.aborted) setRefBusy(false);
            });
        return () => abort.abort();
    }, [ledger, target.ledger, refRefresh]);
    const targetEntity = props.entities.find((entity) => entity.ledger.id === target.ledger);
    const own =
        copy.context?.ledger === ledger &&
        copy.context.draft === review.draft_id &&
        copy.context.file === file;
    const blocked = props.locked || props.dirty || childEditing || busy;
    async function saveTarget(next: { ledger: string; file: string }, applyCopy = false) {
        const context = copy.context;
        setBusy(true);
        try {
            const saved = await saveDraftReview(props.session.csrf_token, ledger, review.draft_id, {
                expected_version: review.version,
                review: {
                    ...review.review,
                    confirmed: props.confirmed,
                    entry: destinationEntry(next, null),
                },
            });
            if (applyCopy && copies.getSnapshot().context !== context) return;
            props.onSaved(saved);
            if (applyCopy) copies.dismiss();
        } catch (error) {
            onError(error);
        } finally {
            setBusy(false);
        }
    }
    async function copyOriginal() {
        try {
            await copies.start(props.session, {
                ledger,
                draft: review.draft_id,
                file,
                targetLedger: choice,
            });
        } catch (error) {
            onError(error);
        }
    }
    if (review.status !== "draft") return null;
    return (
        <>
            <section aria-label={t("票据归属", "Document destination")}>
                <h4>{t("入账账本", "Destination ledger")}</h4>
                <p>{targetEntity?.name ?? target.ledger}</p>
                {target.ledger !== ledger && references?.ledger !== target.ledger && (
                    <button
                        disabled={refBusy || blocked}
                        onClick={() => setRefRefresh((value) => value + 1)}
                    >
                        {t("重新加载目标账户和分类", "Reload destination accounts and categories")}
                    </button>
                )}
                {props.dirty && (
                    <p>
                        {t(
                            "请先保存字段复核，再更改归属。",
                            "Save field reviews before changing the destination.",
                        )}
                    </p>
                )}
                <p>
                    {t(
                        "更改归属先复制原件，再明确采用；原入账草稿的账户和分类将清空，需重新填写。复制不会入账。",
                        "Change destination by copying the original, then explicitly apply it. Prepared accounts and categories will be cleared for review. Copying does not post an entry.",
                    )}
                </p>
                <select
                    aria-label={t("选择目标账本", "Choose destination ledger")}
                    value={choice}
                    disabled={blocked || !!copy.context}
                    onChange={(event) => setChoice(event.target.value)}
                >
                    {props.entities
                        .filter((entity) => !entity.archived || entity.ledger.id === target.ledger)
                        .map((entity) => (
                            <option
                                key={entity.ledger.id}
                                value={entity.ledger.id}
                                disabled={entity.archived}
                            >
                                {entity.name}
                            </option>
                        ))}
                </select>
                <button
                    disabled={blocked || !!copy.context || choice === target.ledger}
                    onClick={() =>
                        choice === ledger ? void saveTarget({ ledger, file }) : void copyOriginal()
                    }
                >
                    {choice === ledger
                        ? t("恢复源账本归属", "Restore source destination")
                        : t("复制原件到目标账本", "Copy original to destination")}
                </button>
                {own && copy.status === "confirmed" && copy.upload.receipt && (
                    <button
                        disabled={
                            blocked ||
                            !props.entities.some(
                                (entity) =>
                                    entity.ledger.id === copy.context!.targetLedger &&
                                    !entity.archived,
                            )
                        }
                        onClick={() =>
                            void saveTarget(
                                {
                                    ledger: copy.context!.targetLedger,
                                    file: copy.upload.receipt!.file_id,
                                },
                                true,
                            )
                        }
                    >
                        {t(
                            "采用目标原件并清空入账草稿",
                            "Apply copied original and clear prepared entry",
                        )}
                    </button>
                )}
            </section>
            <OcrEntryForm
                {...props}
                key={`${review.draft_id}:${target.ledger}`}
                onEditing={setChildEditing}
                accounts={
                    target.ledger === ledger
                        ? props.accounts
                        : references?.ledger === target.ledger
                          ? references.accounts
                          : []
                }
                categories={
                    target.ledger === ledger
                        ? props.categories
                        : references?.ledger === target.ledger
                          ? references.categories
                          : []
                }
                locked={
                    props.locked ||
                    busy ||
                    !!copy.context ||
                    !targetEntity ||
                    targetEntity.archived ||
                    (target.ledger !== ledger && references?.ledger !== target.ledger)
                }
            />
        </>
    );
}
