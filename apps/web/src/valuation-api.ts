import { readJson, writeJson } from "./api";
import type { paths } from "./generated/openapi";

const quotesPath = "/api/v1/quotes/manual";
const reportPath = "/api/v1/reports/mvp";

type Json<T extends { content: { "application/json": unknown } }> =
    T["content"]["application/json"];
type QuoteRoutes = paths[typeof quotesPath];
type ReportRoute = paths[typeof reportPath]["get"];
export type ManualPrice = Json<QuoteRoutes["put"]["responses"][200]>;
export type ManualPriceInput = Json<QuoteRoutes["put"]["requestBody"]>;
type PriceQuery = NonNullable<QuoteRoutes["get"]["parameters"]["query"]>;
type PricePage = Json<QuoteRoutes["get"]["responses"][200]>;
export type MvpReport = Json<ReportRoute["responses"][200]>;
export type ReportQuery = ReportRoute["parameters"]["query"];

function url(path: string, query: object): string {
    const params = new URLSearchParams();
    for (const [name, value] of Object.entries(query)) {
        if (Array.isArray(value)) for (const member of value) params.append(name, String(member));
        else if (value != null) params.set(name, String(value));
    }
    return `${path}?${params}`;
}

export const readManualPrices = (query: PriceQuery, signal?: AbortSignal): Promise<PricePage> =>
    readJson(url(quotesPath, query), signal);

export const saveManualPrice = (csrf: string, draft: ManualPriceInput): Promise<ManualPrice> =>
    writeJson(quotesPath, csrf, draft, "PUT");

export const readMvpReport = (query: ReportQuery, signal?: AbortSignal): Promise<MvpReport> =>
    readJson(url(reportPath, query), signal);
