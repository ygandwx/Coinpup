import { useEffect, useRef, useState } from "react";
import type { Session } from "./api";
import type { Locale } from "./i18n";
import type { Entity } from "./ledger-api";
import { getDraftReview, getOcrDraft, listDraftDuplicates } from "./ocr-api";
import { reviewedInput } from "./ocr-batch";
import type { ConfirmationInput, PendingConfirmationController } from "./pending-confirmations";

export type BatchSelection = { id: string; number: number };
type Item = { number: number; input: ConfirmationInput; duplicates: number; ack: boolean };
export function OcrBatch({
    session,
    locale,
    ledger,
    entities,
    selected,
    locked,
    controller,
    onClear,
    onEditing,
    onError,
}: {
    session: Session;
    locale: Locale;
    ledger: string;
    entities: Entity[];
    selected: BatchSelection[];
    locked: boolean;
    controller: PendingConfirmationController;
    onClear: () => void;
    onEditing: (editing: boolean) => void;
    onError: (error: unknown) => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const [items, setItems] = useState<Item[] | null>(null),
        [reading, setReading] = useState(false);
    const pending = useRef<AbortController | null>(null);
    useEffect(() => () => pending.current?.abort(), []);
    useEffect(() => {
        onEditing(reading || items !== null);
        return () => onEditing(false);
    }, [reading, items, onEditing]);
    async function prepare() {
        pending.current?.abort();
        const abort = new AbortController();
        pending.current = abort;
        setReading(true);
        setItems(null);
        try {
            const next: Item[] = [];
            for (const row of selected) {
                const review = await getDraftReview(ledger, row.id, abort.signal);
                const detail = await getOcrDraft(ledger, row.id, abort.signal);
                const duplicates = await listDraftDuplicates(ledger, row.id, abort.signal);
                if (abort.signal.aborted) return;
                next.push({
                    number: row.number,
                    input: reviewedInput(ledger, detail.file_id, review),
                    duplicates: duplicates.length,
                    ack: false,
                });
            }
            if (!abort.signal.aborted) setItems(next);
        } catch (error) {
            if (!abort.signal.aborted) onError(error);
        } finally {
            if (!abort.signal.aborted) setReading(false);
        }
    }
    function cancel() {
        pending.current?.abort();
        setReading(false);
        setItems(null);
    }
    async function submit() {
        if (!items?.length || locked || items.some((item) => item.duplicates && !item.ack)) return;
        const inputs = items.map((item) => ({
            ...item.input,
            body: { ...item.input.body, duplicate_ack: item.ack },
        }));
        try {
            const flight = controller.start(session, inputs);
            setItems(null);
            onClear();
            await flight;
        } catch (error) {
            onError(error);
        }
    }
    return (
        <section aria-label={t("批量确认", "Batch confirmation")}>
            <h3>{t("批量确认", "Batch confirmation")}</h3>
            <p>
                {t(
                    `已选择 ${selected.length} 行（最多 200）。仅提交已保存且逐项复核的草稿；逐行提交，未知结果暂停。`,
                    `${selected.length} rows selected (maximum 200). Only saved, individually reviewed drafts can be submitted. Entries are sequential; unknown outcomes pause the batch.`,
                )}
            </p>
            {!items && !reading && (
                <button disabled={locked || !selected.length} onClick={() => void prepare()}>
                    {t("核对所选草稿", "Review selected drafts")}
                </button>
            )}
            {reading && (
                <p role="status">
                    {t("正在读取草稿和重复提示。", "Reading drafts and duplicate hints.")}
                </p>
            )}
            {items && (
                <ul className="ocr-list">
                    {items.map((item, index) => {
                        const action = item.input.body.entry;
                        return (
                            <li key={item.input.draftId}>
                                <span>
                                    {t("草稿", "Draft")} {item.number} ·{" "}
                                    {action.kind === "link"
                                        ? t("关联已有流水", "Link existing entry")
                                        : `${"amount" in action.command ? action.command.amount : action.command.source_amount} ${"asset_id" in action.command ? action.command.asset_id : action.command.source_asset_id}`}{" "}
                                    ·{" "}
                                    {entities.find(
                                        (entity) =>
                                            entity.ledger.id === item.input.body.target_ledger_id,
                                    )?.name ?? t("目标账本不可用", "Destination unavailable")}
                                </span>
                                {!!item.duplicates && (
                                    <label>
                                        <input
                                            type="checkbox"
                                            checked={item.ack}
                                            disabled={locked}
                                            onChange={(event) =>
                                                setItems((value) =>
                                                    value!.map((row, position) =>
                                                        position === index
                                                            ? { ...row, ack: event.target.checked }
                                                            : row,
                                                    ),
                                                )
                                            }
                                        />
                                        {t(
                                            "此原件行已有确认；请取消批量核对、打开单行查看历史。已核对并明确同意额外操作",
                                            "This source row was confirmed before. Cancel batch review and open the individual draft to inspect its history. I reviewed it and explicitly allow this additional action",
                                        )}
                                    </label>
                                )}
                            </li>
                        );
                    })}
                </ul>
            )}
            {items && (
                <button
                    disabled={
                        locked ||
                        !items.length ||
                        items.some((item) => item.duplicates > 0 && !item.ack)
                    }
                    onClick={() => void submit()}
                >
                    {t("确认所选草稿", "Confirm selected drafts")}
                </button>
            )}
            {(reading || items) && (
                <button disabled={locked} onClick={cancel}>
                    {t("取消批量核对", "Cancel batch review")}
                </button>
            )}
        </section>
    );
}
