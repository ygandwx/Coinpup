import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type PeriodChange = components["schemas"]["PeriodChange"];
export type PeriodReceipt = components["schemas"]["PeriodChangeResponse"];
export type PeriodState = components["schemas"]["PeriodState"];
const base = (ledgerId: string) => `/api/v1/ledgers/${encodeURIComponent(ledgerId)}`;
export const getPeriod = (ledgerId: string, signal?: AbortSignal): Promise<PeriodState> =>
    readJson(`${base(ledgerId)}/period`, signal);
export const periodHistory = (
    ledgerId: string,
    offset = 0,
    signal?: AbortSignal,
): Promise<PeriodReceipt[]> =>
    readJson(`${base(ledgerId)}/period-changes?limit=25&offset=${offset}`, signal);
export const changePeriod = (
    csrf: string,
    ledgerId: string,
    body: PeriodChange,
    key: string,
): Promise<PeriodReceipt> => writeJson(`${base(ledgerId)}/period-changes`, csrf, body, "POST", key);
