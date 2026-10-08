import { getDocument, GlobalWorkerOptions } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { previewSize } from "./preview-budget";

GlobalWorkerOptions.workerSrc = workerUrl;
export type Preview = {
    pages: number;
    render: (
        canvas: HTMLCanvasElement,
        page: number,
    ) => { done: Promise<void>; cancel: () => void };
    close: () => void;
};
export async function openPreview(blob: Blob, signal: AbortSignal): Promise<Preview> {
    signal.throwIfAborted();
    if (blob.size > 20 * 1024 * 1024) throw new Error("preview_size");
    if (blob.type !== "application/pdf") {
        const bitmap = await createImageBitmap(blob);
        try {
            previewSize(bitmap.width, bitmap.height);
        } catch (error) {
            bitmap.close();
            throw error;
        }
        return {
            pages: 1,
            close: () => bitmap.close(),
            render(canvas) {
                const done = (async () => {
                    const size = previewSize(bitmap.width, bitmap.height);
                    canvas.width = size.width;
                    canvas.height = size.height;
                    const context = canvas.getContext("2d");
                    if (!context) throw new Error("preview_canvas");
                    context.drawImage(bitmap, 0, 0, size.width, size.height);
                })();
                return { done, cancel() {} };
            },
        };
    }
    const task = getDocument({
        data: new Uint8Array(await blob.arrayBuffer()),
        cMapUrl: "/assets/pdfjs/cmaps/",
        cMapPacked: true,
        standardFontDataUrl: "/assets/pdfjs/standard_fonts/",
        useWasm: false,
        disableFontFace: true,
        enableXfa: false,
        disableAutoFetch: true,
        disableStream: true,
        disableRange: true,
        // Do not silently omit high-resolution invoice images from the displayed page.
        maxImageSize: -1,
        canvasMaxAreaInBytes: 16_000_000,
        verbosity: -1,
        stopAtErrors: true,
    });
    const abort = () => {
        void task.destroy().catch(() => {});
    };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    const timer = window.setTimeout(() => {
        void task.destroy().catch(() => {});
    }, 60_000);
    try {
        const document = await task.promise;
        if (document.numPages > 50) throw new Error("preview_pages");
        let rendering: Promise<void> = Promise.resolve();
        return {
            pages: document.numPages,
            close: () => {
                void task.destroy().catch(() => {});
            },
            render(canvas, number) {
                let cancelled = false,
                    cancelRender = () => {};
                const previous = rendering;
                const done = (async () => {
                    await previous.catch(() => {});
                    if (cancelled) return;
                    const page = await document.getPage(number);
                    try {
                        if (cancelled) return;
                        const viewport = page.getViewport({ scale: 1 });
                        const size = previewSize(viewport.width, viewport.height);
                        canvas.width = size.width;
                        canvas.height = size.height;
                        const render = page.render({
                            canvas,
                            viewport: page.getViewport({ scale: size.scale }),
                        });
                        cancelRender = () => render.cancel();
                        const deadline = window.setTimeout(cancelRender, 30_000);
                        try {
                            await render.promise;
                        } finally {
                            window.clearTimeout(deadline);
                        }
                    } finally {
                        page.cleanup();
                    }
                })();
                rendering = done;
                return {
                    done,
                    cancel() {
                        cancelled = true;
                        cancelRender();
                    },
                };
            },
        };
    } catch (error) {
        await task.destroy().catch(() => {});
        throw error;
    } finally {
        window.clearTimeout(timer);
        signal.removeEventListener("abort", abort);
    }
}
