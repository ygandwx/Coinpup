import { ApiError, readBlob, readJson, writeBytes, writeJson } from "./api";
import type { components } from "./generated/openapi";
import type { ArchivedPageOptions, UUID } from "./ledger-api";

export type FileMediaType = components["schemas"]["FileResponse"]["detected_media_type"];
export type FileConfiguration = components["schemas"]["FileConfiguration"];
export type UploadCreate = components["schemas"]["UploadCreate"];
export type UploadCompletion = components["schemas"]["UploadCompletion"];
export type UploadReservation = components["schemas"]["UploadResponse"];
export type StoredFile = components["schemas"]["FileResponse"];
export type OperationFileLink = components["schemas"]["LinkResponse"];
export type FileUpdate = components["schemas"]["FileUpdate"];
export type LinkUpdate = components["schemas"]["LinkUpdate"];

const ledger = (id: UUID) => `/api/v1/ledgers/${encodeURIComponent(id)}`;
async function list<T>(
    path: string,
    options?: ArchivedPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<T[]> {
    const page = options instanceof AbortSignal ? {} : (options ?? {});
    const { limit = 100, offset = 0, include_archived } = page;
    if (
        !Number.isInteger(limit) ||
        limit < 1 ||
        limit > 200 ||
        !Number.isInteger(offset) ||
        offset < 0 ||
        offset > 100000
    ) {
        throw new ApiError("server", null, "invalid_pagination");
    }
    const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (include_archived !== undefined) query.set("include_archived", String(include_archived));
    return readJson<T[]>(`${path}?${query}`, options instanceof AbortSignal ? options : signal);
}

export const getFileConfiguration = (signal?: AbortSignal): Promise<FileConfiguration> =>
    readJson("/api/v1/files/configuration", signal);
export const reserveUpload = (
    csrf: string,
    ledgerId: UUID,
    body: UploadCreate,
    signal?: AbortSignal,
): Promise<UploadReservation> =>
    writeJson(`${ledger(ledgerId)}/uploads`, csrf, body, "POST", undefined, signal);
export const getUpload = (
    ledgerId: UUID,
    uploadId: UUID,
    signal?: AbortSignal,
): Promise<UploadReservation> =>
    readJson(`${ledger(ledgerId)}/uploads/${encodeURIComponent(uploadId)}`, signal);
export const putUploadContent = (
    csrf: string,
    ledgerId: UUID,
    uploadId: UUID,
    file: File,
    options: { timeoutMs: number; signal?: AbortSignal },
): Promise<UploadCompletion> =>
    writeBytes(
        `${ledger(ledgerId)}/uploads/${encodeURIComponent(uploadId)}/content`,
        csrf,
        file,
        options,
    );
export const listFiles = (
    ledgerId: UUID,
    options?: ArchivedPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<StoredFile[]> => list(`${ledger(ledgerId)}/files`, options, signal);
export const getFile = (ledgerId: UUID, fileId: UUID, signal?: AbortSignal): Promise<StoredFile> =>
    readJson(`${ledger(ledgerId)}/files/${encodeURIComponent(fileId)}`, signal);
export const updateFile = (
    csrf: string,
    ledgerId: UUID,
    fileId: UUID,
    body: FileUpdate,
): Promise<StoredFile> =>
    writeJson(`${ledger(ledgerId)}/files/${encodeURIComponent(fileId)}`, csrf, body, "PATCH");
export const listOperationFiles = (
    ledgerId: UUID,
    operationId: UUID,
    options?: ArchivedPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<OperationFileLink[]> =>
    list(
        `${ledger(ledgerId)}/operations/${encodeURIComponent(operationId)}/files`,
        options,
        signal,
    );
export const linkFile = (
    csrf: string,
    ledgerId: UUID,
    operationId: UUID,
    fileId: UUID,
): Promise<OperationFileLink> =>
    writeJson(
        `${ledger(ledgerId)}/operations/${encodeURIComponent(operationId)}/files/${encodeURIComponent(fileId)}`,
        csrf,
        undefined,
        "PUT",
    );
export const updateLink = (
    csrf: string,
    ledgerId: UUID,
    operationId: UUID,
    fileId: UUID,
    body: LinkUpdate,
): Promise<OperationFileLink> =>
    writeJson(
        `${ledger(ledgerId)}/operations/${encodeURIComponent(operationId)}/files/${encodeURIComponent(fileId)}`,
        csrf,
        body,
        "PATCH",
    );
export const downloadFile = async (
    ledgerId: UUID,
    fileId: UUID,
    signal?: AbortSignal,
): Promise<Blob> => {
    const blob = await readBlob(
        `${ledger(ledgerId)}/files/${encodeURIComponent(fileId)}/content`,
        signal,
    );
    if (
        !blob.size ||
        !["application/pdf", "image/jpeg", "image/png", "image/webp"].includes(blob.type)
    )
        throw new ApiError("server", 200, "invalid_response");
    return blob;
};
