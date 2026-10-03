import { ApiError, readBlob, readJson, writeBytes, writeJson } from "./api";
import type { ArchivedPageOptions, UUID } from "./ledger-api";

export type DocumentMediaType = "application/pdf" | "image/jpeg" | "image/png" | "image/webp";
export type FileConfiguration = {
    max_upload_bytes: number;
    upload_timeout_seconds: number;
    supported_media_types: DocumentMediaType[];
};
export type UploadCreate = {
    id: UUID;
    original_filename: string;
    declared_size: number;
    operation_id: UUID | null;
};
export type UploadCompletion = {
    upload_id: UUID;
    file_id: UUID;
    link_id: UUID | null;
    duplicate: boolean;
    sha256: string;
    byte_size: number;
    media_type: DocumentMediaType;
};
type UploadMetadata = {
    id: UUID;
    ledger_id: UUID;
    original_filename: string;
    declared_size: number;
    operation_id: UUID | null;
    created_at: string;
};
export type UploadReservation = UploadMetadata &
    (
        | { state: "pending"; completed_at: null; response: null }
        | { state: "ready"; completed_at: string; response: UploadCompletion }
    );
export type DocumentFile = {
    id: UUID;
    ledger_id: UUID;
    created_by: UUID;
    sha256: string;
    byte_size: number;
    detected_media_type: DocumentMediaType;
    original_filename: string;
    title: string;
    archived: boolean;
    version: number;
    created_at: string;
    updated_at: string;
};
export type OperationFileLink = {
    id: UUID;
    ledger_id: UUID;
    file_id: UUID;
    operation_id: UUID;
    created_by: UUID;
    archived: boolean;
    version: number;
    created_at: string;
    updated_at: string;
};
export type FileUpdate = { expected_version: number; title?: string; archived?: boolean };
export type LinkUpdate = { expected_version: number; archived: boolean };

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
): Promise<DocumentFile[]> => list(`${ledger(ledgerId)}/files`, options, signal);
export const getFile = (
    ledgerId: UUID,
    fileId: UUID,
    signal?: AbortSignal,
): Promise<DocumentFile> =>
    readJson(`${ledger(ledgerId)}/files/${encodeURIComponent(fileId)}`, signal);
export const updateFile = (
    csrf: string,
    ledgerId: UUID,
    fileId: UUID,
    body: FileUpdate,
): Promise<DocumentFile> =>
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
