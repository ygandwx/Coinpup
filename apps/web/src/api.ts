export type Session = {
  user: { id: string | number; username: string };
  csrf_token: string;
};

export type ApiErrorKind = "unauthorized" | "forbidden" | "rate-limited" | "network" | "server";

export class ApiError extends Error {
  constructor(public readonly kind: ApiErrorKind) {
    super(kind);
    this.name = "ApiError";
  }
}

async function request(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<Response> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  const timeout = window.setTimeout(abort, 12_000);

  try {
    const response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      headers: { Accept: "application/json", ...init.headers },
    });
    if (!response.ok) {
      if (response.status === 401) throw new ApiError("unauthorized");
      if (response.status === 403) throw new ApiError("forbidden");
      if (response.status === 429) throw new ApiError("rate-limited");
      throw new ApiError("server");
    }
    return response;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("network");
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

async function parseSession(response: Response): Promise<Session> {
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new ApiError("server");
  }
  if (typeof data !== "object" || data === null) throw new ApiError("server");
  const candidate = data as Partial<Session>;
  if (
    !candidate.user ||
    !["string", "number"].includes(typeof candidate.user.id) ||
    typeof candidate.user.username !== "string" ||
    typeof candidate.csrf_token !== "string" ||
    !candidate.csrf_token
  ) {
    throw new ApiError("server");
  }
  return candidate as Session;
}

export async function getSession(signal?: AbortSignal): Promise<Session> {
  return parseSession(await request("/api/v1/auth/session", {}, signal));
}

export async function signIn(username: string, password: string): Promise<Session> {
  return parseSession(await request("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }));
}

export async function signOut(csrfToken: string): Promise<void> {
  await request("/api/v1/auth/logout", {
    method: "POST",
    headers: { "X-CSRF-Token": csrfToken },
  });
}

export async function checkHealth(kind: "live" | "ready", signal?: AbortSignal): Promise<boolean> {
  const response = await request(`/api/v1/health/${kind}`, {}, signal);
  const data: unknown = await response.json();
  return typeof data === "object" && data !== null && "status" in data && data.status === "ok";
}
