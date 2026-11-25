import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

type Contract = components["schemas"];
export type InterledgerSettings = Contract["InterledgerSettings"];
export type InterledgerAdvance = Contract["InterledgerAdvance"];
export type InterledgerAdvanceCreate = Contract["InterledgerAdvanceCreate"];
export type InterledgerDetail = Contract["InterledgerDetail"];
export type InterledgerRepayment = Contract["InterledgerRepayment"];
export type InterledgerRepaymentCreate = Contract["InterledgerRepaymentCreate"];

const settings = "/api/v1/interledger/settings";
const advances = "/api/v1/interledger/advances";
const advance = (id: string) => `${advances}/${encodeURIComponent(id)}`;

export const getInterledgerSettings = (signal?: AbortSignal): Promise<InterledgerSettings> =>
    readJson(settings, signal);
export const setInterledgerSettings = (
    csrf: string,
    enabled: boolean,
    signal?: AbortSignal,
): Promise<InterledgerSettings> => writeJson(settings, csrf, { enabled }, "PUT", undefined, signal);
export const getInterledgerAdvances = (
    offset: number,
    limit: number,
    signal?: AbortSignal,
): Promise<InterledgerAdvance[]> => readJson(`${advances}?limit=${limit}&offset=${offset}`, signal);
export const getInterledgerAdvance = (
    id: string,
    signal?: AbortSignal,
): Promise<InterledgerDetail> => readJson(advance(id), signal);
export const registerAdvance = (
    csrf: string,
    body: InterledgerAdvanceCreate,
    key: string,
): Promise<InterledgerAdvance> => writeJson(advances, csrf, body, "POST", key);
export const recordRepayment = (
    csrf: string,
    id: string,
    body: InterledgerRepaymentCreate,
    key: string,
): Promise<InterledgerRepayment> => writeJson(`${advance(id)}/repayments`, csrf, body, "POST", key);
