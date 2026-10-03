export type Session = {
  user: { id: string | number; username: string };
  csrf_token: string;
};

export type ApiErrorKind = "unauthorized" | "forbidden" | "rate-limited" | "network" | "server";
export type FieldError = { path: (string | number)[]; message: string; type: string };

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly code: string | null;
  readonly fieldErrors: FieldError[];

  constructor(kind: ApiErrorKind, status: number | null = null, code: string | null = null, fieldErrors: FieldError[] = []) {
    super(code ?? kind);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.fieldErrors = fieldErrors;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function apiPath(path: string): void {
  // Validate both raw and decoded paths; URL normalization must not escape /api/v1/.
  try {
    if (!path.startsWith("/api/v1/") || /[\\#\s]/u.test(path)) throw new Error();
    const parsed = new URL(path, "https://coinpup.invalid");
    const decoded = decodeURIComponent(parsed.pathname);
    const normalized = new URL(decoded, "https://coinpup.invalid");
    if (parsed.origin !== "https://coinpup.invalid" || normalized.origin !== parsed.origin ||
      !parsed.pathname.startsWith("/api/v1/") || !normalized.pathname.startsWith("/api/v1/") ||
      decoded.includes("\\")) throw new Error();
  } catch {
    throw new ApiError("server", null, "invalid_api_path");
  }
}

function responseError(status: number, data: unknown): ApiError {
  const kind = status === 401 ? "unauthorized" : status === 403 ? "forbidden" : status === 429 ? "rate-limited" : "server";
  const detail = isRecord(data) ? data.detail : undefined;
  const code = isRecord(detail) && typeof detail.code === "string" ? detail.code : status === 422 ? "validation_error" : null;
  const fields: FieldError[] = [];
  if (Array.isArray(detail)) {
    for (const item of detail) {
      if (isRecord(item) && Array.isArray(item.loc) && item.loc.every((part) => typeof part === "string" || typeof part === "number") &&
        typeof item.msg === "string" && typeof item.type === "string") {
        // Do not copy validation input/context: they can contain private submitted data.
        fields.push({ path: item.loc, message: item.msg, type: item.type });
      }
    }
  }
  return new ApiError(kind, status, code, fields);
}

async function requestJSON<T>(path: string, init: RequestInit = {}, signal?: AbortSignal, empty = false, timeoutMs = 12_000, binary = false): Promise<T> {
  apiPath(path);
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 3_610_000) throw new ApiError("server", null, "invalid_timeout");
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  const timeout = globalThis.setTimeout(abort, timeoutMs);

  try {
    const response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
      headers: { Accept: "application/json", ...init.headers },
    });
    if (response.ok && empty) return undefined as T;
    if (response.ok && binary) return await response.blob() as T;
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      if (controller.signal.aborted) throw new ApiError("network");
      if (!response.ok) throw responseError(response.status, null);
      throw new ApiError("server", response.status, "invalid_response");
    }
    if (!response.ok) throw responseError(response.status, data);
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("network");
  } finally {
    globalThis.clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

export function readJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return requestJSON<T>(path, {}, signal);
}

export async function writeJson<T>(path: string, csrfToken: string, body: unknown, method: "POST" | "PATCH" | "PUT" = "POST", idempotencyKey?: string, signal?: AbortSignal): Promise<T> {
  return requestJSON<T>(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
      ...(idempotencyKey === undefined ? {} : { "Idempotency-Key": idempotencyKey }),
    },
    body: JSON.stringify(body),
  }, signal);
}

export function writeBytes<T>(path: string, csrfToken: string, body: Blob, options: { timeoutMs: number; signal?: AbortSignal }): Promise<T> {
  return requestJSON<T>(path, {
    method: "PUT", body,
    headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": csrfToken },
  }, options.signal, false, options.timeoutMs);
}

export function readBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  return requestJSON<Blob>(path, { headers: { Accept: "application/octet-stream" } }, signal, false, 120_000, true);
}

function parseSession(data: unknown): Session {
  if (!isRecord(data)) throw new ApiError("server", null, "invalid_response");
  const candidate = data as Partial<Session>;
  if (!candidate.user || !["string", "number"].includes(typeof candidate.user.id) ||
    typeof candidate.user.username !== "string" || typeof candidate.csrf_token !== "string" || !candidate.csrf_token) {
    throw new ApiError("server", null, "invalid_response");
  }
  return candidate as Session;
}

export async function getSession(signal?: AbortSignal): Promise<Session> {
  return parseSession(await readJson<unknown>("/api/v1/auth/session", signal));
}

export async function signIn(username: string, password: string): Promise<Session> {
  return parseSession(await requestJSON<unknown>("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }));
}

export async function signOut(csrfToken: string): Promise<void> {
  await requestJSON<void>("/api/v1/auth/logout", {
    method: "POST",
    headers: { "X-CSRF-Token": csrfToken },
  }, undefined, true);
}

export async function checkHealth(kind: "live" | "ready", signal?: AbortSignal): Promise<boolean> {
  const data = await readJson<unknown>(`/api/v1/health/${kind}`, signal);
  return isRecord(data) && data.status === "ok";
}
