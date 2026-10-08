import { ApiError } from "./api";
import type { Locale } from "./i18n";

export function ocrError(error: unknown, locale: Locale): string {
    const zh = locale === "zh";
    const code = error instanceof ApiError ? error.code : null;
    if (code === "ocr_batch_not_ready")
        return zh
            ? "所选草稿尚未全部准备好。请逐行复核字段并保存入账草稿后重试。"
            : "Selected drafts are not all ready. Review each row and save its prepared entry before retrying.";
    if (code === "ocr_unavailable")
        return zh
            ? "识别服务暂不可用。可稍后重试或手动记账。"
            : "Recognition is unavailable. Retry later or post manually.";
    if (code === "version_conflict")
        return zh
            ? "草稿或关联流水已被修改。请核对服务器版本后重新编辑。"
            : "The draft or linked entry changed. Review the server version before editing again.";
    if (code === "ocr_duplicate_confirmation")
        return zh
            ? "此原件行已有确认记录，请先核对重复提示。"
            : "This source row was confirmed before. Review the duplicate warning.";
    if (code === "ocr_review_required")
        return zh ? "请逐项核对所有待确认字段。" : "Review every required field individually.";
    return zh
        ? "暂时无法完成操作。输入已保留，请核对状态后重试。"
        : "The action could not be completed. Your input is retained; check the state and retry.";
}
