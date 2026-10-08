import { ApiError } from "./api";
import type { Session } from "./api";
import { downloadFile, getFile, getFileConfiguration } from "./files-api";
import type { StoredFile } from "./files-api";
import { PendingUploadController, uploadTransport } from "./pending-upload";
import type { PendingUploadSnapshot, UploadTransport } from "./pending-upload";

export type CopySource = Readonly<{
    ledger: string;
    draft: string;
    file: string;
    targetLedger: string;
}>;
type Context = CopySource & { owner: string };
type Snapshot = Readonly<{
    context: Context | null;
    status: PendingUploadSnapshot["status"] | "reading" | "read-failed";
    upload: PendingUploadSnapshot;
    error: ApiError | null;
}>;
type Reads = {
    metadata: typeof getFile;
    content: typeof downloadFile;
    configuration: typeof getFileConfiguration;
};
const reads: Reads = {
    metadata: getFile,
    content: downloadFile,
    configuration: getFileConfiguration,
};

/** Keeps the source draft alongside the existing two-phase upload intent, only in App memory. */
export class OcrCopyController {
    private owner: string | null = null;
    private context: Context | null = null;
    private source: StoredFile | null = null;
    private upload: PendingUploadController;
    private reading = false;
    private error: ApiError | null = null;
    private abort: AbortController | null = null;
    private flight: Promise<void> | null = null;
    private generation = 0;
    private listeners = new Set<() => void>();
    private snapshot: Snapshot;
    private io: Reads;
    constructor(io: Reads = reads, transport: UploadTransport = uploadTransport) {
        this.io = io;
        this.upload = new PendingUploadController({
            ...transport,
            upload: async (...args) => {
                const expected = this.source;
                const receipt = await transport.upload(...args);
                if (
                    !expected ||
                    receipt.sha256 !== expected.sha256 ||
                    receipt.byte_size !== expected.byte_size ||
                    receipt.media_type !== expected.detected_media_type
                )
                    throw new ApiError("server", 200, "copy_identity_mismatch");
                return receipt;
            },
        });
        this.snapshot = this.current();
        this.upload.subscribe(() => this.publish());
    }
    getSnapshot = (): Snapshot => this.snapshot;
    subscribe = (listener: () => void) => {
        this.listeners.add(listener);
        return () => {
            this.listeners.delete(listener);
        };
    };
    private current(): Snapshot {
        const upload = this.upload.getSnapshot();
        const hidden = this.owner === null && this.context !== null;
        return Object.freeze({
            context: hidden ? null : this.context,
            upload,
            status: hidden
                ? "auth-required"
                : this.reading
                  ? "reading"
                  : this.error
                    ? "read-failed"
                    : upload.status,
            error: hidden ? null : (this.error ?? upload.error),
        });
    }
    private publish() {
        this.snapshot = this.current();
        for (const listener of [...this.listeners]) listener();
    }
    private clearRead() {
        this.generation += 1;
        this.abort?.abort();
        this.abort = null;
        this.flight = null;
        this.reading = false;
    }
    setOwner(owner: string | null, explicitLogout = false) {
        if (owner === this.owner && !explicitLogout) return;
        const reset =
            explicitLogout ||
            (owner !== null && this.context !== null && owner !== this.context.owner);
        if (reset) {
            this.clearRead();
            this.context = null;
            this.source = null;
            this.error = null;
        } else if (owner === null && this.reading) {
            this.clearRead();
            this.error = new ApiError("unauthorized", 401);
        }
        this.owner = owner;
        if (owner !== null && this.error?.kind === "unauthorized")
            this.error = new ApiError("network");
        this.upload.setOwner(owner, explicitLogout);
        this.publish();
    }
    private authorize(session: Session) {
        if (
            String(session.user.id) !== this.owner ||
            (this.context && this.context.owner !== this.owner)
        )
            throw new ApiError("unauthorized", 401, "pending_owner_mismatch");
    }
    start(session: Session, source: CopySource): Promise<void> {
        this.authorize(session);
        if (this.context) throw new ApiError("server", null, "copy_unresolved");
        if (source.ledger === source.targetLedger)
            throw new ApiError("server", 422, "copy_same_ledger");
        this.context = Object.freeze({ ...source, owner: String(session.user.id) });
        return this.readAndUpload(session);
    }
    retry(session: Session): Promise<void> {
        this.authorize(session);
        if (this.flight) return this.flight;
        if (!this.context) throw new ApiError("server", null, "copy_missing");
        // A read failure has no server write to replay. An upload always reuses its original File/UUID.
        return this.upload.getSnapshot().status === "idle"
            ? this.readAndUpload(session)
            : this.upload.retry(session);
    }
    dismiss = (): boolean => {
        if (this.reading || !this.upload.dismiss()) return false;
        this.clearRead();
        this.context = null;
        this.source = null;
        this.error = null;
        this.publish();
        return true;
    };
    private readAndUpload(session: Session): Promise<void> {
        const context = this.context!,
            generation = this.generation,
            abort = new AbortController();
        this.abort = abort;
        this.reading = true;
        this.error = null;
        this.publish();
        const flight = Promise.resolve().then(async () => {
            try {
                const [metadata, configuration] = await Promise.all([
                    this.io.metadata(context.ledger, context.file, abort.signal),
                    this.io.configuration(abort.signal),
                ]);
                if (abort.signal.aborted || generation !== this.generation) return;
                if (
                    metadata.id !== context.file ||
                    metadata.ledger_id !== context.ledger ||
                    metadata.archived ||
                    metadata.byte_size > configuration.max_upload_bytes
                )
                    throw new ApiError("server", 422, "copy_source_invalid");
                const blob = await this.io.content(context.ledger, context.file, abort.signal);
                if (abort.signal.aborted || generation !== this.generation) return;
                if (blob.size !== metadata.byte_size || blob.type !== metadata.detected_media_type)
                    throw new ApiError("server", 200, "copy_identity_mismatch");
                this.authorize(session);
                this.source = Object.freeze({ ...metadata });
                const file = new File([blob], metadata.original_filename, { type: blob.type });
                this.reading = false;
                await this.upload.submit(session, context.targetLedger, { file }, configuration);
            } catch (error) {
                if (generation === this.generation && !abort.signal.aborted)
                    this.error = error instanceof ApiError ? error : new ApiError("network");
            } finally {
                if (generation === this.generation) {
                    this.reading = false;
                    this.flight = null;
                    this.abort = null;
                    this.publish();
                }
            }
        });
        this.flight = flight;
        return flight;
    }
}
