import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { downloadFile } from "./files-api";
import type { Locale } from "./i18n";
import type { InvoiceComplete } from "./invoice-issuance-receipt";
import type { DeepReadonly } from "./settlement-intent";

type Result = DeepReadonly<InvoiceComplete>;
export async function checkedOriginalBlob(
    blob: Blob,
    file: Result["artifacts"][number],
    signal: AbortSignal,
) {
    signal.throwIfAborted();
    if (
        blob.type !== "application/pdf" ||
        blob.type !== file.media_type ||
        blob.size !== file.byte_size ||
        blob.size > 8388608
    )
        throw new ApiError("server", 200, "download_integrity_error");
    const bytes = await blob.arrayBuffer();
    signal.throwIfAborted();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    signal.throwIfAborted();
    if (
        [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("") !==
        file.sha256
    )
        throw new ApiError("server", 200, "download_integrity_error");
    return blob;
}
// Caller keys this component to its authorized ledger + immutable result; no private URL is
// attached to a persistent link or exposed before the exact byte count and digest match.
export function InvoiceOriginals({
    ledgerId,
    result,
    locale,
    onUnauthorized,
}: {
    ledgerId: string;
    result: Result;
    locale: Locale;
    onUnauthorized: () => void;
}) {
    const t = (zh: string, en: string) => (locale === "zh" ? zh : en);
    const active = useRef<AbortController | null>(null),
        urls = useRef(new Map<string, number>()),
        latest = useRef({ result, ledgerId, onUnauthorized });
    latest.current = { result, ledgerId, onUnauthorized };
    const [busy, setBusy] = useState(false),
        [failed, setFailed] = useState(false);
    useEffect(() => {
        const owned = urls.current;
        return () => {
            active.current?.abort();
            for (const [url, timer] of owned) {
                window.clearTimeout(timer);
                URL.revokeObjectURL(url);
            }
            owned.clear();
        };
    }, [ledgerId, result.id]);
    async function save(file: Result["artifacts"][number]) {
        if (active.current || result.ledger_id !== ledgerId) return;
        const ctrl = new AbortController();
        active.current = ctrl;
        setBusy(true);
        setFailed(false);
        const current = () =>
            !ctrl.signal.aborted &&
            latest.current.result.id === result.id &&
            latest.current.ledgerId === ledgerId;
        try {
            const blob = await checkedOriginalBlob(
                await downloadFile(ledgerId, file.file_id, ctrl.signal),
                file,
                ctrl.signal,
            );
            if (!current()) return;
            const url = URL.createObjectURL(blob);
            try {
                const anchor = document.createElement("a");
                anchor.href = url;
                anchor.download = `Invoice-${result.sequence}-${file.language}.pdf`;
                anchor.hidden = true;
                document.body.append(anchor);
                try {
                    anchor.click();
                } finally {
                    anchor.remove();
                }
            } finally {
                urls.current.set(
                    url,
                    window.setTimeout(() => {
                        URL.revokeObjectURL(url);
                        urls.current.delete(url);
                    }, 1000),
                );
            }
        } catch (e) {
            if (current()) {
                if (e instanceof ApiError && e.kind === "unauthorized")
                    latest.current.onUnauthorized();
                else setFailed(true);
            }
        } finally {
            if (active.current === ctrl) {
                active.current = null;
                if (current()) setBusy(false);
            }
        }
    }
    return (
        <div>
            {result.artifacts.map((file) => (
                <button
                    className="secondary-button"
                    key={file.file_id}
                    type="button"
                    disabled={busy}
                    onClick={() => void save(file)}
                >
                    {file.language === "zh"
                        ? t("下载中文原件", "Download Chinese original")
                        : t("下载英文原件", "Download English original")}
                </button>
            ))}
            {failed && (
                <p role="alert">
                    {t(
                        "无法核对原件，未下载文件。",
                        "The original could not be verified. No file was downloaded.",
                    )}
                </p>
            )}
        </div>
    );
}
