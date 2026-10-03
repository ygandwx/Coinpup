import { ApiError, readJson, writeJson } from "./api";
import type { components, operations } from "./generated/openapi";

export type UUID = components["schemas"]["EntityResponse"]["id"];
export type Quantity = components["schemas"]["OperationResponse"]["amount"];
export type CalendarDate = components["schemas"]["OpeningCreate"]["transaction_date"];
export type AccountKind = components["schemas"]["AccountCreate"]["kind"];
export type CategoryKind = components["schemas"]["CategoryCreate"]["kind"];
export type TemplateKey = components["schemas"]["TemplateResponse"]["key"];
export type CountryCode = NonNullable<components["schemas"]["EntityCreate"]["country_code"]>;
export type AssetKind = components["schemas"]["AssetCreate"]["kind"];
export type RecordVersion = Pick<
    components["schemas"]["EntityResponse"],
    "version" | "created_at" | "updated_at"
>;

export type Asset = components["schemas"]["AssetResponse"];
export type Ledger = components["schemas"]["LedgerResponse"];
export type Entity = components["schemas"]["EntityResponse"];
export type Account = components["schemas"]["AccountResponse"];
export type Category = components["schemas"]["CategoryResponse"];
export type TemplateCategory = components["schemas"]["TemplateCategory"];
export type CategoryTemplate = components["schemas"]["TemplateResponse"];
export type Balance = components["schemas"]["BalanceResponse"];
export type AssetResponse = Asset;
export type LedgerResponse = Ledger;
export type EntityResponse = Entity;
export type AccountResponse = Account;
export type CategoryResponse = Category;
export type TemplateResponse = CategoryTemplate;
export type BalanceResponse = Balance;

export type EntityCreate = components["schemas"]["EntityCreate"];
export type EntityUpdate = components["schemas"]["EntityUpdate"];
export type AccountCreate = components["schemas"]["AccountCreate"];
export type AccountUpdate = components["schemas"]["AccountUpdate"];
export type CategoryCreate = components["schemas"]["CategoryCreate"];
export type CategoryUpdate = components["schemas"]["CategoryUpdate"];
export type AssetCreate = components["schemas"]["AssetCreate"];
export type AssetUpdate = components["schemas"]["AssetUpdate"];
export type EntityCreateBody = EntityCreate;
export type EntityUpdateBody = EntityUpdate;
export type AccountCreateBody = AccountCreate;
export type AccountUpdateBody = AccountUpdate;
export type CategoryCreateBody = CategoryCreate;
export type CategoryUpdateBody = CategoryUpdate;

export type PostingSplit = components["schemas"]["PostingSplit"];
export type FeeCreate = components["schemas"]["FeeCreate"];
export type FeeResponse = components["schemas"]["FeeResponse"];
export type OpeningCreate = components["schemas"]["OpeningCreate"];
export type IncomeCreate = components["schemas"]["IncomeCreate"];
export type ExpenseCreate = components["schemas"]["ExpenseCreate"];
export type TransferCreate = components["schemas"]["TransferCreate"];
export type ExchangeCreate = components["schemas"]["ExchangeCreate"];
export type OperationResponse = components["schemas"]["OperationResponse"];
export type TransferResponse = components["schemas"]["TransferResponse"];
export type ExchangeResponse = components["schemas"]["ExchangeResponse"];
export type FinancialResponse = components["schemas"]["OperationState"]["latest_posting"];
export type Replacement = components["schemas"]["CorrectionCreate"]["replacement"];
export type CorrectionCreate = components["schemas"]["CorrectionCreate"];
export type CancellationCreate = components["schemas"]["CancellationCreate"];
export type CancellationInfo = components["schemas"]["CancellationInfo"];
export type OperationState = components["schemas"]["OperationState"];
export type LineAudit = components["schemas"]["LineAudit"];
export type JournalAudit = components["schemas"]["JournalAudit"];
export type HistoryEntry = components["schemas"]["HistoryEntry"];

export type PageOptions = NonNullable<
    operations["operation_history_api_v1_ledgers__ledger_id__operations__operation_id__history_get"]["parameters"]["query"]
>;
export type ArchivedPageOptions = NonNullable<
    operations["list_entities_api_v1_entities_get"]["parameters"]["query"]
>;
export type AssetPageOptions = NonNullable<
    operations["list_assets_api_v1_assets_get"]["parameters"]["query"]
>;
export type BalancePageOptions = NonNullable<
    operations["balances_api_v1_ledgers__ledger_id__balances_get"]["parameters"]["query"]
>;
export type OperationPageOptions = NonNullable<
    operations["list_operations_api_v1_ledgers__ledger_id__operations_get"]["parameters"]["query"]
>;
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
