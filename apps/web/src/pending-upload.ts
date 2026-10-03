import { ApiError } from "./api";
import type { Session } from "./api";
import { getUpload, putUploadContent, reserveUpload } from "./document-api";
import type {
    FileConfiguration,
    UploadCompletion,
    UploadCreate,
    UploadReservation,
} from "./document-api";

export type UploadInput = { file: File; operationId?: string | null };
export type PendingUploadStatus =
    | "idle"
    | "reserving"
    | "uploading"
    | "unknown"
    | "auth-required"
    | "rejected"
    | "confirmed"
    | "upload-conflict";
export type PendingUploadCommand = Readonly<{
    ownerId: string;
    ledgerId: string;
    uploadId: string;
    operationId: string | null;
    filename: string;
    byteSize: number;
    manifestJson: string;
}>;
export type PendingUploadSnapshot = Readonly<{
    status: PendingUploadStatus;
    command: PendingUploadCommand | null;
    receipt: UploadCompletion | null;
    reservation: UploadReservation | null;
    error: ApiError | null;
    checking: boolean;
}>;
export type UploadTransport = {
    reserve(
        session: Session,
        command: PendingUploadCommand,
        signal: AbortSignal,
    ): Promise<UploadReservation>;
    upload(
        session: Session,
        command: PendingUploadCommand,
        file: File,
        options: { timeoutMs: number; signal: AbortSignal },
    ): Promise<UploadCompletion>;
    read(
        session: Session,
        command: PendingUploadCommand,
        signal: AbortSignal,
    ): Promise<UploadReservation>;
};
const transport: UploadTransport = {
    reserve(session, command, signal) {
        return reserveUpload(
            session.csrf_token,
            command.ledgerId,
            JSON.parse(command.manifestJson) as UploadCreate,
            signal,
        );
    },
    upload(session, command, file, options) {
        return putUploadContent(
            session.csrf_token,
            command.ledgerId,
            command.uploadId,
            file,
            options,
        );
    },
    read(_session, command, signal) {
        return getUpload(command.ledgerId, command.uploadId, signal);
    },
};
const idle = (): PendingUploadSnapshot =>
    Object.freeze({
        status: "idle",
        command: null,
        receipt: null,
        reservation: null,
        error: null,
        checking: false,
    });
const record = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
const uuid = (value: unknown): value is string =>
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/iu.test(value);
const media = ["application/pdf", "image/jpeg", "image/png", "image/webp"];
function validCompletion(value: unknown, command: PendingUploadCommand): value is UploadCompletion {
    return (
        record(value) &&
        value.upload_id === command.uploadId &&
        uuid(value.file_id) &&
        (command.operationId === null ? value.link_id === null : uuid(value.link_id)) &&
        typeof value.duplicate === "boolean" &&
        typeof value.sha256 === "string" &&
        /^[0-9a-f]{64}(?![\s\S])/u.test(value.sha256) &&
        value.byte_size === command.byteSize &&
        typeof value.media_type === "string" &&
        media.includes(value.media_type)
    );
}
function validReservation(
    value: unknown,
    command: PendingUploadCommand,
): value is UploadReservation {
    if (
        !record(value) ||
        value.id !== command.uploadId ||
        value.ledger_id !== command.ledgerId ||
        value.original_filename !== command.filename ||
        value.declared_size !== command.byteSize ||
        value.operation_id !== command.operationId ||
        typeof value.created_at !== "string"
    )
        return false;
    return value.state === "pending"
        ? value.response === null && value.completed_at === null
        : value.state === "ready" &&
              typeof value.completed_at === "string" &&
              validCompletion(value.response, command);
}
const apiError = (error: unknown) => (error instanceof ApiError ? error : new ApiError("network"));

/** App-owned, memory-only upload intent. The File never enters public snapshots or browser storage. */
export class PendingUploadController {
    private owner: string | null = null;
    private state: PendingUploadSnapshot = idle();
    private visible = this.state;
    private listeners = new Set<() => void>();
    private file: File | null = null;
    private timeoutMs = 130_000;
    private generation = 0;
    private uncertain = false;
    private conflict: ApiError | null = null;
    private flight: Promise<void> | null = null;
    private abort: AbortController | null = null;
    private io: UploadTransport;

    constructor(io: UploadTransport = transport) {
        this.io = io;
    }
    getSnapshot = (): PendingUploadSnapshot => this.visible;
    subscribe = (listener: () => void): (() => void) => {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    };
    private publish(next = this.state): void {
        this.state = Object.freeze(next);
        this.visible =
            this.owner === null && next.command !== null
                ? Object.freeze({ ...idle(), status: "auth-required" })
                : this.state;
        for (const listener of [...this.listeners]) listener();
    }
    private reset(): void {
        this.generation += 1;
        this.abort?.abort();
        this.abort = null;
        this.flight = null;
        this.file = null;
        this.uncertain = false;
        this.conflict = null;
        this.state = idle();
    }
    setOwner(ownerId: string | null, explicitLogout = false): void {
        if (!explicitLogout && ownerId === this.owner) return;
        const resuming =
            !explicitLogout &&
            this.owner === null &&
            ownerId !== null &&
            ownerId === this.state.command?.ownerId;
        if (
            explicitLogout ||
            (ownerId !== null &&
                this.state.command !== null &&
                ownerId !== this.state.command.ownerId)
        )
            this.reset();
        if (resuming && this.state.status === "auth-required") {
            this.uncertain = true;
            this.state = {
                ...this.state,
                status: this.conflict ? "upload-conflict" : "unknown",
                error: this.conflict,
            };
        } else if (resuming && this.state.error?.kind === "unauthorized")
            this.state = { ...this.state, error: null };
        this.owner = ownerId;
        this.publish();
    }
    dismiss = (): boolean => {
        if (this.state.command !== null && !["confirmed", "rejected"].includes(this.state.status))
            return false;
        this.reset();
        this.publish();
        return true;
    };
    private authorized(session: Session): void {
        const owner = String(session.user.id);
        if (
            this.owner !== owner ||
            (this.state.command !== null && this.state.command.ownerId !== owner)
        )
            throw new ApiError("unauthorized", 401, "pending_owner_mismatch");
    }
    submit(
        session: Session,
        ledgerId: string,
        input: UploadInput,
        configuration: FileConfiguration,
    ): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight !== null) return this.flight;
            if (this.state.command !== null)
                throw new ApiError("server", null, "pending_upload_unresolved");
            if (
                !Number.isSafeInteger(configuration.max_upload_bytes) ||
                configuration.max_upload_bytes < 1 ||
                !Number.isSafeInteger(configuration.upload_timeout_seconds) ||
                configuration.upload_timeout_seconds < 1 ||
                configuration.upload_timeout_seconds > 3600
            )
                throw new ApiError("server", null, "invalid_configuration");
            if (
                !(input.file instanceof File) ||
                input.file.size < 1 ||
                input.file.size > configuration.max_upload_bytes
            )
                throw new ApiError("server", 413, "file_too_large");
            if (
                !input.file.name.trim() ||
                [...input.file.name].length > 255 ||
                /[\p{Cc}/\\]/u.test(input.file.name) ||
                [".", ".."].includes(input.file.name)
            )
                throw new ApiError("server", 422, "invalid_filename");
            if (!uuid(ledgerId) || (input.operationId != null && !uuid(input.operationId)))
                throw new ApiError("server", 422, "invalid_upload_reference");
            const uploadId = crypto.randomUUID();
            const manifest: UploadCreate = {
                id: uploadId,
                original_filename: input.file.name,
                declared_size: input.file.size,
                operation_id: input.operationId ?? null,
            };
            const command: PendingUploadCommand = Object.freeze({
                ownerId: String(session.user.id),
                ledgerId,
                uploadId,
                operationId: manifest.operation_id,
                filename: manifest.original_filename,
                byteSize: manifest.declared_size,
                manifestJson: JSON.stringify(manifest),
            });
            this.file = input.file;
            this.timeoutMs = configuration.upload_timeout_seconds * 1000 + 10_000;
            this.publish({ ...idle(), status: "reserving", command });
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    retry(session: Session): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight !== null) return this.flight;
            if (
                !this.file ||
                !this.state.command ||
                !["unknown", "auth-required"].includes(this.state.status)
            )
                throw new ApiError("server", null, "pending_retry_unavailable");
            return this.send(session);
        } catch (error) {
            return Promise.reject(error);
        }
    }
    stop = (): boolean => {
        if (!this.flight || !["reserving", "uploading"].includes(this.state.status)) return false;
        this.uncertain = true;
        this.abort?.abort();
        this.publish({
            ...this.state,
            status: "unknown",
            error: new ApiError("network", null, "upload_interrupted"),
        });
        return true;
    };
    private send(session: Session): Promise<void> {
        const command = this.state.command!;
        const file = this.file!;
        const generation = this.generation;
        const abort = new AbortController();
        this.abort = abort;
        this.publish({ ...this.state, status: "reserving", error: null });
        const flight = Promise.resolve().then(async () => {
            if (generation !== this.generation) return;
            try {
                const reservation = await this.io.reserve(session, command, abort.signal);
                if (generation !== this.generation) return;
                if (!validReservation(reservation, command))
                    throw new ApiError("server", 201, "invalid_response");
                if (abort.signal.aborted) throw new ApiError("network", null, "upload_interrupted");
                this.publish({ ...this.state, reservation, status: "uploading" });
                // Even a ready reservation is retried with the exact original File, proving its bytes.
                const receipt = await this.io.upload(session, command, file, {
                    timeoutMs: this.timeoutMs,
                    signal: abort.signal,
                });
                if (generation !== this.generation) return;
                if (!validCompletion(receipt, command))
                    throw new ApiError("server", 200, "invalid_response");
                this.uncertain = false;
                this.file = null;
                this.publish({ ...this.state, status: "confirmed", receipt, error: null });
            } catch (problem) {
                if (generation !== this.generation) return;
                const error = apiError(problem);
                let status: PendingUploadStatus;
                if (error.kind === "unauthorized") {
                    status = "auth-required";
                    this.uncertain = true;
                } else if (
                    ["upload_manifest_conflict", "upload_content_conflict"].includes(
                        error.code ?? "",
                    )
                ) {
                    status = "upload-conflict";
                    this.conflict = error;
                } else if (
                    !this.uncertain &&
                    error.status !== null &&
                    [400, 403, 404, 409, 413, 415, 422, 429].includes(error.status)
                ) {
                    status = "rejected";
                    this.file = null;
                } else {
                    status = "unknown";
                    this.uncertain = true;
                }
                this.publish({ ...this.state, status, error });
            } finally {
                if (generation === this.generation) {
                    this.flight = null;
                    this.abort = null;
                }
            }
        });
        this.flight = flight;
        return flight;
    }
    reconcile(session: Session): Promise<void> {
        try {
            this.authorized(session);
            if (this.flight !== null) return this.flight;
            if (!this.state.command || this.state.status === "rejected")
                throw new ApiError("server", null, "pending_check_unavailable");
            const command = this.state.command;
            const generation = this.generation;
            const abort = new AbortController();
            this.abort = abort;
            this.publish({ ...this.state, checking: true });
            const flight = Promise.resolve().then(async () => {
                if (generation !== this.generation) return;
                try {
                    const reservation = await this.io.read(session, command, abort.signal);
                    if (generation !== this.generation) return;
                    if (!validReservation(reservation, command))
                        throw new ApiError("server", 200, "invalid_response");
                    if (reservation.state === "ready" && this.conflict === null) {
                        this.uncertain = false;
                        this.file = null;
                        this.publish({
                            ...this.state,
                            status: "confirmed",
                            reservation,
                            receipt: reservation.response,
                            error: null,
                            checking: false,
                        });
                    } else {
                        const status = this.conflict
                            ? "upload-conflict"
                            : ["confirmed", "rejected"].includes(this.state.status)
                              ? this.state.status
                              : "unknown";
                        this.publish({ ...this.state, status, reservation, checking: false });
                    }
                } catch (problem) {
                    if (generation !== this.generation) return;
                    const error = apiError(problem);
                    const status =
                        this.state.status === "confirmed"
                            ? "confirmed"
                            : error.kind === "unauthorized"
                              ? "auth-required"
                              : this.state.status === "upload-conflict"
                                ? "upload-conflict"
                                : "unknown";
                    if (status === "unknown") this.uncertain = true;
                    this.publish({ ...this.state, status, error, checking: false });
                } finally {
                    if (generation === this.generation) {
                        this.flight = null;
                        this.abort = null;
                    }
                }
            });
            this.flight = flight;
            return flight;
        } catch (error) {
            return Promise.reject(error);
        }
    }
}
