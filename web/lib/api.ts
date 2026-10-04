// apiFetch (DESIGN §4.9): same origin, X-ThriftRadar on every call (the local CSRF guard needs it on mutations),
// clear errors for 503 (warming up), 429 (retry later) and 401 (demo login).

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public retryAfter: number | null = null,
  ) {
    super(detail);
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-ThriftRadar", "1");
  if (init.body && typeof init.body === "string") headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (res.status === 204) return undefined as T;
  if (res.ok) return (await res.json()) as T;
  if (res.status === 401 && typeof window !== "undefined") {
    window.location.href = "/login/";
  }
  let detail = res.statusText;
  try {
    const body = await res.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* not JSON */
  }
  const ra = res.headers.get("Retry-After");
  throw new ApiError(res.status, detail, ra ? Number(ra) : null);
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 503) return "The models are still loading. This takes about a minute after the server starts.";
    if (e.status === 429) return `Too many requests. Try again in ${e.retryAfter ?? 60} seconds.`;
    if (e.status === 409 && e.detail === "too_many_wishlists") return "You already have 10 wishlists. Remove one first.";
    if (e.status === 422 && e.detail.startsWith("invalid_image")) return "That file isn't a photo we can read (JPEG, PNG or WebP, up to 5 MB).";
    return `Something went wrong (${e.status}: ${e.detail}).`;
  }
  return "Can't reach the ThriftRadar server. Is it running on 127.0.0.1:8000?";
}
