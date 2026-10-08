import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { downloadFile } from "./files-api";
import type { Locale } from "./i18n";
import type { Preview } from "./pdf-preview";

export function OriginalPreview({
    ledger,
    file,
    locale,
    onUnauthorized,
}: {
    ledger: string;
    file: string;
    locale: Locale;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const canvas = useRef<HTMLCanvasElement>(null),
        unauthorized = useRef(onUnauthorized);
    const [document, setDocument] = useState<Preview | null>(null),
        [page, setPage] = useState(1);
    const [ready, setReady] = useState(false),
        [failed, setFailed] = useState(false);
    function choosePage(value: number) {
        if (value === page) return;
        setReady(false);
        setPage(value);
    }
    useEffect(() => {
        unauthorized.current = onUnauthorized;
    }, [onUnauthorized]);
    useEffect(() => {
        const abort = new AbortController();
        let opened: Preview | null = null;
        setDocument(null);
        setPage(1);
        setReady(false);
        setFailed(false);
        void (async () => {
            const blob = await downloadFile(ledger, file, abort.signal);
            if (abort.signal.aborted) return;
            const { openPreview } = await import("./pdf-preview");
            const next = await openPreview(blob, abort.signal);
            if (abort.signal.aborted) {
                next.close();
                return;
            }
            opened = next;
            setDocument(next);
        })().catch((error) => {
            if (abort.signal.aborted) return;
            if (error instanceof ApiError && error.kind === "unauthorized") unauthorized.current();
            else setFailed(true);
        });
        return () => {
            abort.abort();
            opened?.close();
        };
    }, [ledger, file]);
    useEffect(() => {
        if (!document || !canvas.current) return;
        let active = true;
        setReady(false);
        setFailed(false);
        const task = document.render(canvas.current, page);
        void task.done
            .then(() => {
                if (active) setReady(true);
            })
            .catch(() => {
                if (active) setFailed(true);
            });
        return () => {
            active = false;
            task.cancel();
        };
    }, [document, page]);
    return (
        <section className="ocr-preview" aria-label={t("原件预览", "Original preview")}>
            <h4>{t("原件预览", "Original preview")}</h4>
            {failed ? (
                <p role="alert">
                    {t(
                        "无法预览此原件，请使用下载按钮对照。预览上限为 20 MiB、50 页。",
                        "Preview unavailable. Use the original download for comparison. Preview limit: 20 MiB and 50 pages.",
                    )}
                </p>
            ) : (
                !ready && <p role="status">{t("正在加载原件…", "Loading original…")}</p>
            )}
            <canvas
                ref={canvas}
                hidden={!ready || failed}
                role="img"
                aria-label={t(`原件第 ${page} 页`, `Original page ${page}`)}
                data-preview-ready={ready}
            />
            {document && (
                <div className="ocr-actions">
                    <button disabled={page <= 1} onClick={() => choosePage(page - 1)}>
                        {t("原件上一页", "Previous original page")}
                    </button>
                    <span>
                        {page} / {document.pages}
                    </span>
                    <select
                        aria-label={t("选择原件页码", "Choose original page")}
                        value={page}
                        onChange={(event) => choosePage(Number(event.target.value))}
                    >
                        {Array.from({ length: document.pages }, (_, index) => (
                            <option key={index} value={index + 1}>
                                {index + 1}
                            </option>
                        ))}
                    </select>
                    <button disabled={page >= document.pages} onClick={() => choosePage(page + 1)}>
                        {t("原件下一页", "Next original page")}
                    </button>
                </div>
            )}
        </section>
    );
}
