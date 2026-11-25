import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

type Schema = components["schemas"];
export type DeliverySettings = Schema["ConfigureDelivery"];
export type DeliveryView = Schema["ReminderDeliveryView"];
export type EmailDelivery = Schema["ReminderEmailView"];

const path = (ledger: string, event: string) =>
    `/api/v1/ledgers/${encodeURIComponent(ledger)}/reminders/${encodeURIComponent(event)}`;

export const readDelivery = (
    ledger: string,
    event: string,
    signal?: AbortSignal,
): Promise<DeliveryView> => readJson(path(ledger, event) + "/delivery-config", signal);

export const writeDelivery = (
    csrf: string,
    ledger: string,
    event: string,
    body: DeliverySettings,
) => writeJson<DeliveryView>(path(ledger, event) + "/delivery-config", csrf, body, "PUT");

export const listEmailDeliveries = (
    ledger: string,
    event: string,
    offset: number,
    limit: number,
    signal?: AbortSignal,
): Promise<EmailDelivery[]> =>
    readJson(path(ledger, event) + `/email-deliveries?offset=${offset}&limit=${limit}`, signal);
