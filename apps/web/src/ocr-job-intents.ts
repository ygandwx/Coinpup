import { ApiError } from "./api";
import type { Session } from "./api";
import { startOcrJob } from "./ocr-api";
import type { OcrJob } from "./ocr-api";

/** Job creation keeps its original UUID across unknown outcomes and same-owner login. */
export class OcrJobIntents {
    private owner: string | null = null;
    private account: string | null = null;
    private generation = 0;
    private intents = new Map<string, string>();
    setOwner(owner: string | null, logout = false): void {
        if (logout || (owner !== null && owner !== this.account)) {
            this.intents.clear();
            this.generation++;
        }
        if (owner !== null || logout) this.account = owner;
        this.owner = owner;
    }
    async start(session: Session, ledger: string, file: string): Promise<OcrJob> {
        if (this.owner !== String(session.user.id)) throw new ApiError("unauthorized", 401);
        const key = `${ledger}/${file}`,
            generation = this.generation;
        const intent = this.intents.get(key) ?? crypto.randomUUID();
        this.intents.set(key, intent);
        const job = await startOcrJob(session.csrf_token, ledger, {
            intent_id: intent,
            file_id: file,
        });
        if (generation !== this.generation || this.owner !== String(session.user.id))
            throw new ApiError("unauthorized", 401);
        if (job.intent_id !== intent || job.ledger_id !== ledger || job.file_id !== file)
            throw new ApiError("server", 200, "invalid_response");
        this.intents.delete(key);
        return job;
    }
}
