import { ApiError } from "./api";
import type { Session } from "./api";
import { getOperation, postExchange, postExpense, postIncome, postOpening, postTransfer } from "./ledger-api";
import type { ExchangeCreate, ExpenseCreate, FinancialResponse, IncomeCreate, OpeningCreate, OperationState, TransferCreate } from "./ledger-api";

export type PostingInput =
  | { kind: "opening"; body: Omit<OpeningCreate, "id"> }
  | { kind: "income"; body: Omit<IncomeCreate, "id"> }
  | { kind: "expense"; body: Omit<ExpenseCreate, "id"> }
  | { kind: "transfer"; body: Omit<TransferCreate, "id"> }
  | { kind: "exchange"; body: Omit<ExchangeCreate, "id"> };
export type PendingStatus = "idle" | "submitting" | "unknown" | "auth-required" | "rejected" | "confirmed" | "idempotency-conflict";
export type PendingCommand = Readonly<{
  ownerId: string; ledgerId: string; operationId: string; key: string;
  kind: PostingInput["kind"]; bodyJson: string;
}>;
export type PendingSnapshot = Readonly<{
  status: PendingStatus; command: PendingCommand | null; receipt: FinancialResponse | null;
  current: OperationState | null; error: ApiError | null; checking: boolean;
}>;
export type PostingTransport = {
  post(session: Session, command: PendingCommand): Promise<FinancialResponse>;
  read(session: Session, command: PendingCommand): Promise<OperationState>;
};

const transport: PostingTransport = {
  post(session, command) {
    // Decode an immutable snapshot, never the current editable form or selected ledger.
    const body = JSON.parse(command.bodyJson);
    const args = [session.csrf_token, command.ledgerId, body, command.key] as const;
    switch (command.kind) {
      case "opening": return postOpening(...args);
      case "income": return postIncome(...args);
      case "expense": return postExpense(...args);
      case "transfer": return postTransfer(...args);
      case "exchange": return postExchange(...args);
    }
  },
  read(_session, command) { return getOperation(command.ledgerId, command.operationId); },
};

const idle = (): PendingSnapshot => Object.freeze({ status: "idle", command: null, receipt: null, current: null, error: null, checking: false });
function record(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
const quantity = (value: unknown) => typeof value === "string" && value.length <= 40 && /^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/u.test(value);
function validReceipt(value: unknown, command: PendingCommand): value is FinancialResponse {
  if (!record(value) || value.id !== command.operationId || value.ledger_id !== command.ledgerId || value.kind !== command.kind ||
    typeof value.version !== "number" || !Number.isInteger(value.version) || value.version < 1 ||
    typeof value.journal_id !== "string" || typeof value.transaction_date !== "string" || typeof value.recognition_date !== "string" ||
    typeof value.description !== "string" || typeof value.created_at !== "string") return false;
  if (value.fees !== undefined && (!Array.isArray(value.fees) || !value.fees.every((fee) => record(fee) && typeof fee.account_id === "string" &&
    typeof fee.asset_id === "string" && typeof fee.category_id === "string" && quantity(fee.amount)))) return false;
  if (value.kind === "exchange") return typeof value.source_account_id === "string" && typeof value.destination_account_id === "string" &&
    typeof value.source_asset_id === "string" && typeof value.destination_asset_id === "string" && quantity(value.source_amount) && quantity(value.destination_amount);
  if (typeof value.asset_id !== "string" || !quantity(value.amount)) return false;
  if (value.kind === "transfer") return typeof value.source_account_id === "string" && typeof value.destination_account_id === "string";
  return typeof value.account_id === "string" && Array.isArray(value.splits) && value.splits.every((split) => record(split) && typeof split.category_id === "string" && quantity(split.amount));
}
function validState(value: unknown, command: PendingCommand): value is OperationState {
  if (!record(value) || value.id !== command.operationId || value.ledger_id !== command.ledgerId || value.kind !== command.kind ||
    typeof value.version !== "number" || !Number.isInteger(value.version) || typeof value.updated_at !== "string" || !validReceipt(value.latest_posting, command)) return false;
  if (value.status === "active") return value.cancellation === undefined && value.version === value.latest_posting.version;
  return value.status === "cancelled" && record(value.cancellation) && value.cancellation.version === value.version &&
    value.version === value.latest_posting.version + 1 && typeof value.cancellation.reversal_journal_id === "string" &&
    typeof value.cancellation.reason === "string" && typeof value.cancellation.recorded_at === "string";
}
function apiError(error: unknown): ApiError { return error instanceof ApiError ? error : new ApiError("network"); }

/** One unresolved command per App instance. No browser storage, global singleton or CSRF retention. */
export class PendingCommandController {
  private owner: string | null = null;
  private state: PendingSnapshot = idle();
  private visible: PendingSnapshot = this.state;
  private listeners = new Set<() => void>();
  private generation = 0;
  private uncertain = false;
  private flight: Promise<void> | null = null;
  private io: PostingTransport;

  constructor(io: PostingTransport = transport) { this.io = io; }
  getSnapshot = (): PendingSnapshot => this.visible;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };

  private publish(next: PendingSnapshot = this.state): void {
    this.state = Object.freeze(next);
    this.visible = this.owner === null && next.command !== null
      ? Object.freeze({ status: "auth-required", command: null, receipt: null, current: null, error: null, checking: false })
      : this.state;
    for (const listener of [...this.listeners]) listener();
  }
  private reset(): void {
    this.generation += 1;
    this.flight = null;
    this.uncertain = false;
    this.state = idle();
  }
  setOwner(ownerId: string | null, explicitLogout = false): void {
    if (!explicitLogout && ownerId === this.owner) return;
    const resuming = !explicitLogout && this.owner === null && ownerId !== null && ownerId === this.state.command?.ownerId;
    if (explicitLogout || (ownerId !== null && this.state.command !== null && ownerId !== this.state.command.ownerId)) this.reset();
    if (resuming) {
      // A fresh same-owner session unlocks manual recovery, not another logout effect.
      if (this.state.status === "auth-required") {
        this.uncertain = true;
        this.state = { ...this.state, status: "unknown", error: null };
      } else if (this.state.error?.kind === "unauthorized") this.state = { ...this.state, error: null };
    }
    this.owner = ownerId;
    this.publish();
  }
  /** Uncertain submissions cannot be dismissed into a fresh key. Explicit logout is separate. */
  dismiss = (): boolean => {
    if (this.state.command !== null && this.state.status !== "confirmed" && this.state.status !== "rejected") return false;
    this.reset();
    this.publish();
    return true;
  };
  clear = (): boolean => this.dismiss();
  private authorized(session: Session): void {
    const id = String(session.user.id);
    if (this.owner !== id || (this.state.command !== null && this.state.command.ownerId !== id)) throw new ApiError("unauthorized", 401, "pending_owner_mismatch");
  }

  submit(session: Session, ledgerId: string, input: PostingInput): Promise<void> {
    try {
      this.authorized(session);
      if (this.flight !== null) return this.flight;
      if (this.state.command !== null) throw new ApiError("server", null, "pending_command_unresolved");
      const operationId = crypto.randomUUID();
      const command: PendingCommand = Object.freeze({ ownerId: String(session.user.id), ledgerId, operationId,
        key: crypto.randomUUID(), kind: input.kind, bodyJson: JSON.stringify({ ...input.body, id: operationId }) });
      this.publish({ status: "submitting", command, receipt: null, current: null, error: null, checking: false });
      return this.send(session);
    } catch (error) { return Promise.reject(error); }
  }

  retry(session: Session): Promise<void> {
    try {
      this.authorized(session);
      if (this.flight !== null) return this.flight;
      if (this.state.command === null || !["unknown", "auth-required"].includes(this.state.status)) throw new ApiError("server", null, "pending_retry_unavailable");
      return this.send(session);
    } catch (error) { return Promise.reject(error); }
  }

  private send(session: Session): Promise<void> {
    const command = this.state.command!;
    const generation = this.generation;
    this.publish({ ...this.state, status: "submitting", error: null });
    // Start in a microtask so flight exists before transport callbacks or repeated clicks run.
    const flight = Promise.resolve().then(async () => {
      if (generation !== this.generation) return;
      try {
        const receipt = await this.io.post(session, command);
        if (generation !== this.generation) return;
        if (!validReceipt(receipt, command)) throw new ApiError("server", 201, "invalid_response");
        this.uncertain = false;
        this.publish({ ...this.state, status: "confirmed", receipt, error: null });
      } catch (problem) {
        if (generation !== this.generation) return;
        const error = apiError(problem);
        let status: PendingStatus;
        if (error.kind === "unauthorized") status = "auth-required";
        else if (error.code === "idempotency_conflict") status = "idempotency-conflict";
        else if (!this.uncertain && error.status !== null && [400, 403, 404, 409, 422, 429].includes(error.status)) status = "rejected";
        else { status = "unknown"; this.uncertain = true; }
        this.publish({ ...this.state, status, error });
      } finally {
        if (generation === this.generation) this.flight = null;
      }
    });
    this.flight = flight;
    return flight;
  }

  reconcile(session: Session): Promise<void> {
    try {
      this.authorized(session);
      if (this.flight !== null) return this.flight;
      if (this.state.command === null) throw new ApiError("server", null, "pending_check_unavailable");
      const command = this.state.command;
      const generation = this.generation;
      this.publish({ ...this.state, checking: true });
      const flight = Promise.resolve().then(async () => {
        if (generation !== this.generation) return;
        try {
          const current = await this.io.read(session, command);
          if (generation !== this.generation) return;
          if (!validState(current, command)) throw new ApiError("server", 200, "invalid_response");
          this.uncertain = false;
          this.publish({ ...this.state, status: "confirmed", current, error: null, checking: false });
        } catch (problem) {
          if (generation !== this.generation) return;
          const error = apiError(problem);
          // A 404 can race an in-flight commit. Only an actual receipt/state confirms outcome.
          const status = this.state.status === "confirmed" ? "confirmed" : error.kind === "unauthorized" ? "auth-required" :
            this.state.status === "idempotency-conflict" ? "idempotency-conflict" : "unknown";
          if (status === "unknown") this.uncertain = true;
          this.publish({ ...this.state, status, error, checking: false });
        } finally {
          if (generation === this.generation) this.flight = null;
        }
      });
      this.flight = flight;
      return flight;
    } catch (error) { return Promise.reject(error); }
  }
}
