import { readJson, writeJson } from "./api";
import type { components } from "./generated/openapi";

export type Party = components["schemas"]["PartyResponse"];
export type Project = components["schemas"]["ProjectResponse"];
export type PartyCreate = components["schemas"]["PartyCreate"];
export type PartyUpdate = components["schemas"]["PartyUpdate"];
export type ProjectCreate = components["schemas"]["ProjectCreate"];
export type ProjectUpdate = components["schemas"]["ProjectUpdate"];
export type MasterKind = "parties" | "projects";
export type MasterRecord = Party | Project;
export type MasterBody = PartyCreate | PartyUpdate | ProjectCreate | ProjectUpdate;
const base = (ledger: string, kind: MasterKind) =>
    `/api/v1/ledgers/${encodeURIComponent(ledger)}/business-${kind}`;
export const listMasterData = (
    ledger: string,
    kind: MasterKind,
    offset = 0,
    signal?: AbortSignal,
): Promise<MasterRecord[]> =>
    readJson(`${base(ledger, kind)}?limit=25&offset=${offset}&include_archived=true`, signal);
export const getMasterData = (
    ledger: string,
    kind: MasterKind,
    id: string,
    signal?: AbortSignal,
): Promise<MasterRecord> => readJson(`${base(ledger, kind)}/${encodeURIComponent(id)}`, signal);
export const saveMasterData = (
    csrf: string,
    ledger: string,
    kind: MasterKind,
    id: string,
    method: "POST" | "PATCH",
    body: MasterBody,
): Promise<MasterRecord> =>
    writeJson(
        base(ledger, kind) + (method === "PATCH" ? `/${encodeURIComponent(id)}` : ""),
        csrf,
        body,
        method,
    );
