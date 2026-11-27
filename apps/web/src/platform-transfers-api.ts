import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

type Contract = components["schemas"];
export type PlatformTransferCreate = Contract["PlatformTransferCreate"];
export type PlatformTransferReceipt = Contract["PlatformTransferReceipt"];
export type PlatformTransferDetail = Contract["PlatformTransferDetail"];
export type PlatformTransferCancel = Contract["PlatformTransferCancel"];
export type PlatformTransferCancellation = Contract["PlatformTransferCancellation"];

const transfers = (ledgerId: string) =>
    `/api/v1/ledgers/${encodeURIComponent(ledgerId)}/platform-transfers`;
const transfer = (ledgerId: string, id: string) =>
    `${transfers(ledgerId)}/${encodeURIComponent(id)}`;

export const listPlatformTransfers = (
    ledgerId: string,
    limit: number,
    offset: number,
    signal?: AbortSignal,
): Promise<PlatformTransferDetail[]> =>
    readJson(`${transfers(ledgerId)}?limit=${limit}&offset=${offset}`, signal);

export const getPlatformTransfer = (
    ledgerId: string,
    id: string,
    signal?: AbortSignal,
): Promise<PlatformTransferDetail> => readJson(transfer(ledgerId, id), signal);

export const createPlatformTransfer = (
    csrf: string,
    ledgerId: string,
    body: PlatformTransferCreate,
    key: string,
): Promise<PlatformTransferReceipt> => writeJson(transfers(ledgerId), csrf, body, "POST", key);

export const cancelPlatformTransfer = (
    csrf: string,
    ledgerId: string,
    id: string,
    body: PlatformTransferCancel,
    key: string,
): Promise<PlatformTransferCancellation> =>
    writeJson(`${transfer(ledgerId, id)}/cancel`, csrf, body, "POST", key);
