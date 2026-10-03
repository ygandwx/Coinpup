import { ApiError, readJson, writeJson } from "./api";

// Wire quantities and calendar dates intentionally remain strings.
export type UUID = string;
export type Quantity = string;
export type CalendarDate = string;
export type AccountKind =
    "bank" | "cash" | "wechat" | "alipay" | "credit_card" | "paypal" | "wise" | "stripe" | "crypto";
export type CategoryKind = "income" | "expense";
export type TemplateKey = "personal_default" | "business_default";
export type CountryCode = "CN" | "HK" | "US" | "EE";
export type AssetKind = "fiat" | "native" | "token";
export type RecordVersion = { version: number; created_at: string; updated_at: string };

export type Asset = RecordVersion & {
    asset_id: string;
    code: string;
    kind: AssetKind;
    scale: number;
    network: string | null;
    token_reference: string | null;
    enabled: boolean;
};
export type Ledger = RecordVersion & { id: UUID; entity_id: UUID; base_asset_id: string };
export type Entity = RecordVersion & {
    id: UUID;
    kind: "personal" | "company";
    name: string;
    legal_name: string | null;
    country_code: CountryCode | null;
    region_code: string | null;
    company_type: string | null;
    registration_date: CalendarDate | null;
    details: Record<string, string>;
    archived: boolean;
    ledger: Ledger;
};
export type Account = RecordVersion & {
    id: UUID;
    ledger_id: UUID;
    name: string;
    kind: AccountKind;
    details: Record<string, string>;
    archived: boolean;
    asset_ids: string[];
};
export type Category = RecordVersion & {
    id: UUID;
    ledger_id: UUID;
    name: string;
    name_en: string | null;
    kind: CategoryKind;
    parent_id: UUID | null;
    template_key: string | null;
    archived: boolean;
};
export type TemplateCategory = { key: string; name: string; name_en: string; kind: CategoryKind };
export type CategoryTemplate = {
    key: TemplateKey;
    name: string;
    name_en: string;
    categories: TemplateCategory[];
};
export type Balance = {
    account_id: UUID;
    asset_id: string;
    amount: Quantity;
    account_archived: boolean;
    asset_enabled: boolean;
    link_enabled: boolean;
};
export type AssetResponse = Asset;
export type LedgerResponse = Ledger;
export type EntityResponse = Entity;
export type AccountResponse = Account;
export type CategoryResponse = Category;
export type TemplateResponse = CategoryTemplate;
export type BalanceResponse = Balance;

export type EntityCreate = {
    id?: UUID | null;
    ledger_id?: UUID | null;
    kind: "personal" | "company";
    name: string;
    legal_name?: string | null;
    country_code?: CountryCode | null;
    region_code?: string | null;
    company_type?: string | null;
    registration_date?: CalendarDate | null;
    details?: Record<string, string>;
    base_asset_id: string;
    template_key?: TemplateKey | null;
    locale?: "zh" | "en";
};
export type EntityUpdate = Partial<
    Pick<
        EntityCreate,
        | "name"
        | "legal_name"
        | "country_code"
        | "region_code"
        | "company_type"
        | "registration_date"
        | "details"
    >
> & {
    expected_version: number;
    archived?: boolean;
};
export type AccountCreate = {
    id?: UUID | null;
    name: string;
    kind: AccountKind;
    details?: Record<string, string>;
    asset_ids: string[];
};
export type AccountUpdate = Partial<Omit<AccountCreate, "id">> & {
    expected_version: number;
    archived?: boolean;
};
export type CategoryCreate = {
    id?: UUID | null;
    name: string;
    name_en?: string | null;
    kind: CategoryKind;
    parent_id?: UUID | null;
};
export type CategoryUpdate = Partial<Pick<CategoryCreate, "name" | "name_en">> & {
    expected_version: number;
    archived?: boolean;
};
export type AssetCreate = {
    code: string;
    kind: AssetKind;
    scale: number;
    network?: string | null;
    token_reference?: string | null;
    enabled?: boolean;
};
export type AssetUpdate = { expected_version: number; enabled: boolean };
export type EntityCreateBody = EntityCreate;
export type EntityUpdateBody = EntityUpdate;
export type AccountCreateBody = AccountCreate;
export type AccountUpdateBody = AccountUpdate;
export type CategoryCreateBody = CategoryCreate;
export type CategoryUpdateBody = CategoryUpdate;

export type PostingSplit = { category_id: UUID; amount: Quantity };
export type FeeCreate = { account_id: UUID; asset_id: string; amount: Quantity; category_id: UUID };
export type FeeResponse = FeeCreate;
type PostingMetadata = { id?: UUID | null; transaction_date: CalendarDate; description?: string };
export type OpeningCreate = PostingMetadata & {
    account_id: UUID;
    asset_id: string;
    amount: Quantity;
};
export type IncomeCreate = OpeningCreate & {
    recognition_date: CalendarDate;
    splits: PostingSplit[];
    fees?: FeeCreate[];
};
export type ExpenseCreate = IncomeCreate;
export type TransferCreate = PostingMetadata & {
    source_account_id: UUID;
    destination_account_id: UUID;
    asset_id: string;
    amount: Quantity;
    fees?: FeeCreate[];
};
export type ExchangeCreate = PostingMetadata & {
    source_account_id: UUID;
    source_asset_id: string;
    source_amount: Quantity;
    destination_account_id: UUID;
    destination_asset_id: string;
    destination_amount: Quantity;
    fees?: FeeCreate[];
};
type FinancialMetadata = {
    id: UUID;
    ledger_id: UUID;
    journal_id: UUID;
    version: number;
    transaction_date: CalendarDate;
    recognition_date: CalendarDate;
    description: string;
    created_at: string;
};
export type OperationResponse = FinancialMetadata & {
    kind: "opening" | "income" | "expense";
    account_id: UUID;
    asset_id: string;
    amount: Quantity;
    splits: PostingSplit[];
    fees?: FeeResponse[];
};
export type TransferResponse = FinancialMetadata & {
    kind: "transfer";
    source_account_id: UUID;
    destination_account_id: UUID;
    asset_id: string;
    amount: Quantity;
    fees?: FeeResponse[];
};
export type ExchangeResponse = FinancialMetadata & {
    kind: "exchange";
    source_account_id: UUID;
    source_asset_id: string;
    source_amount: Quantity;
    destination_account_id: UUID;
    destination_asset_id: string;
    destination_amount: Quantity;
    fees?: FeeResponse[];
};
export type FinancialResponse = OperationResponse | TransferResponse | ExchangeResponse;
export type Replacement =
    | (Omit<OpeningCreate, "id"> & { kind: "opening" })
    | (Omit<IncomeCreate, "id"> & { kind: "income" })
    | (Omit<ExpenseCreate, "id"> & { kind: "expense" })
    | (Omit<TransferCreate, "id"> & { kind: "transfer" })
    | (Omit<ExchangeCreate, "id"> & { kind: "exchange" });
export type CorrectionCreate = {
    expected_version: number;
    reason: string;
    replacement: Replacement;
};
export type CancellationCreate = { expected_version: number; reason: string };
export type CancellationInfo = {
    version: number;
    reversal_journal_id: UUID;
    reason: string;
    recorded_at: string;
};
type StateMetadata = {
    id: UUID;
    ledger_id: UUID;
    kind: FinancialResponse["kind"];
    version: number;
    latest_posting: FinancialResponse;
    updated_at: string;
};
export type OperationState = StateMetadata &
    (
        | { status: "active"; cancellation?: never }
        | { status: "cancelled"; cancellation: CancellationInfo }
    );
export type LineAudit = {
    id: UUID;
    line_no: number;
    component_no: number;
    role: string;
    asset_id: string;
    amount: Quantity;
    account_id: UUID | null;
    category_id: UUID | null;
};
export type JournalAudit = {
    id: UUID;
    kind: "posting" | "reversal";
    reverses_journal_id: UUID | null;
    transaction_date: CalendarDate;
    recognition_date: CalendarDate;
    description: string;
    reason: string | null;
    recorded_at: string;
    lines: LineAudit[];
};
export type HistoryEntry = {
    version: number;
    action: "create" | "correct" | "cancel";
    actor_id: UUID;
    reason: string | null;
    recorded_at: string;
    journals: JournalAudit[];
};

export type PageOptions = { limit?: number; offset?: number };
export type ArchivedPageOptions = PageOptions & { include_archived?: boolean };
export type AssetPageOptions = PageOptions & { include_disabled?: boolean };
export type BalancePageOptions = PageOptions & { account_id?: UUID };
export type OperationPageOptions = PageOptions & { status?: "all" | "active" | "cancelled" };
export type { PostingInput } from "./pending-command";

function pageQuery(options: PageOptions & Record<string, unknown>): string {
    const { limit = 100, offset = 0, ...filters } = options;
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
    for (const [key, value] of Object.entries(filters)) {
        if (value !== undefined) query.set(key, String(value));
    }
    return `?${query.toString()}`;
}

async function list<T, O extends PageOptions>(
    path: string,
    options: O | AbortSignal | undefined,
    signal?: AbortSignal,
): Promise<T[]> {
    const requestSignal = options instanceof AbortSignal ? options : signal;
    const page = options instanceof AbortSignal ? {} : (options ?? {});
    return readJson<T[]>(path + pageQuery(page), requestSignal);
}
const ledgerPath = (id: UUID) => `/api/v1/ledgers/${encodeURIComponent(id)}`;

export const listEntities = (
    options?: ArchivedPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<Entity[]> => list("/api/v1/entities", options, signal);
export const getEntity = (id: UUID, signal?: AbortSignal): Promise<Entity> =>
    readJson(`/api/v1/entities/${encodeURIComponent(id)}`, signal);
export const createEntity = (csrf: string, body: EntityCreate): Promise<Entity> =>
    writeJson("/api/v1/entities", csrf, body);
export const updateEntity = (csrf: string, id: UUID, body: EntityUpdate): Promise<Entity> =>
    writeJson(`/api/v1/entities/${encodeURIComponent(id)}`, csrf, body, "PATCH");
export const listAssets = (
    options?: AssetPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<Asset[]> => list("/api/v1/assets", options, signal);
export const listTemplates = (signal?: AbortSignal): Promise<CategoryTemplate[]> =>
    readJson("/api/v1/category-templates", signal);
export const listAccounts = (
    ledgerId: UUID,
    options?: ArchivedPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<Account[]> => list(`${ledgerPath(ledgerId)}/accounts`, options, signal);
export const createAccount = (
    csrf: string,
    ledgerId: UUID,
    body: AccountCreate,
): Promise<Account> => writeJson(`${ledgerPath(ledgerId)}/accounts`, csrf, body);
export const updateAccount = (
    csrf: string,
    ledgerId: UUID,
    id: UUID,
    body: AccountUpdate,
): Promise<Account> =>
    writeJson(`${ledgerPath(ledgerId)}/accounts/${encodeURIComponent(id)}`, csrf, body, "PATCH");
export const listCategories = (
    ledgerId: UUID,
    options?: ArchivedPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<Category[]> => list(`${ledgerPath(ledgerId)}/categories`, options, signal);
export const createCategory = (
    csrf: string,
    ledgerId: UUID,
    body: CategoryCreate,
): Promise<Category> => writeJson(`${ledgerPath(ledgerId)}/categories`, csrf, body);
export const updateCategory = (
    csrf: string,
    ledgerId: UUID,
    id: UUID,
    body: CategoryUpdate,
): Promise<Category> =>
    writeJson(`${ledgerPath(ledgerId)}/categories/${encodeURIComponent(id)}`, csrf, body, "PATCH");
export const listBalances = (
    ledgerId: UUID,
    options?: BalancePageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<Balance[]> => list(`${ledgerPath(ledgerId)}/balances`, options, signal);
export const createAsset = (csrf: string, body: AssetCreate): Promise<Asset> =>
    writeJson("/api/v1/assets", csrf, body);
export const updateAsset = (csrf: string, assetId: string, body: AssetUpdate): Promise<Asset> =>
    writeJson(`/api/v1/assets/${encodeURIComponent(assetId)}`, csrf, body, "PATCH");
export const postOpening = (
    csrf: string,
    ledgerId: UUID,
    body: OpeningCreate,
    key: string,
): Promise<OperationResponse> =>
    writeJson(`${ledgerPath(ledgerId)}/opening-balances`, csrf, body, "POST", key);
export const postIncome = (
    csrf: string,
    ledgerId: UUID,
    body: IncomeCreate,
    key: string,
): Promise<OperationResponse> =>
    writeJson(`${ledgerPath(ledgerId)}/income`, csrf, body, "POST", key);
export const postExpense = (
    csrf: string,
    ledgerId: UUID,
    body: ExpenseCreate,
    key: string,
): Promise<OperationResponse> =>
    writeJson(`${ledgerPath(ledgerId)}/expenses`, csrf, body, "POST", key);
export const postTransfer = (
    csrf: string,
    ledgerId: UUID,
    body: TransferCreate,
    key: string,
): Promise<TransferResponse> =>
    writeJson(`${ledgerPath(ledgerId)}/transfers`, csrf, body, "POST", key);
export const postExchange = (
    csrf: string,
    ledgerId: UUID,
    body: ExchangeCreate,
    key: string,
): Promise<ExchangeResponse> =>
    writeJson(`${ledgerPath(ledgerId)}/exchanges`, csrf, body, "POST", key);
export const listOperations = (
    ledgerId: UUID,
    options?: OperationPageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<OperationState[]> => list(`${ledgerPath(ledgerId)}/operations`, options, signal);
export const getOperation = (
    ledgerId: UUID,
    operationId: UUID,
    signal?: AbortSignal,
): Promise<OperationState> =>
    readJson(`${ledgerPath(ledgerId)}/operations/${encodeURIComponent(operationId)}`, signal);
export const correctOperation = (
    csrf: string,
    ledgerId: UUID,
    operationId: UUID,
    body: CorrectionCreate,
    key: string,
): Promise<OperationState> =>
    writeJson(
        `${ledgerPath(ledgerId)}/operations/${encodeURIComponent(operationId)}/corrections`,
        csrf,
        body,
        "POST",
        key,
    );
export const cancelOperation = (
    csrf: string,
    ledgerId: UUID,
    operationId: UUID,
    body: CancellationCreate,
    key: string,
): Promise<OperationState> =>
    writeJson(
        `${ledgerPath(ledgerId)}/operations/${encodeURIComponent(operationId)}/cancellations`,
        csrf,
        body,
        "POST",
        key,
    );
export const getOperationHistory = (
    ledgerId: UUID,
    operationId: UUID,
    options?: PageOptions | AbortSignal,
    signal?: AbortSignal,
): Promise<HistoryEntry[]> =>
    list(
        `${ledgerPath(ledgerId)}/operations/${encodeURIComponent(operationId)}/history`,
        options,
        signal,
    );
