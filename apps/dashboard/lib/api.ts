export type FetchResult<T> = {
  data: T;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/**
 * Every screen calls this — it always hits /api/proxy/*, which forwards
 * to the real FastAPI backend (see app/api/proxy/[...path]/route.ts).
 * There is no mock/fixture path here: an unreachable backend surfaces as
 * a real error, not fallback fake data.
 */
export async function apiGet<T>(path: string): Promise<FetchResult<T>> {
  const res = await fetch(`/api/proxy/${path}`, { cache: "no-store" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail || `${path} failed with ${res.status}`);
  }
  const data = (await res.json()) as T;
  return { data };
}

export async function apiPost<T>(path: string, body: unknown): Promise<FetchResult<T>> {
  const res = await fetch(`/api/proxy/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const respBody = await res.json().catch(() => ({}));
    throw new ApiError(res.status, respBody.detail || `${path} failed with ${res.status}`);
  }
  const data = (await res.json()) as T;
  return { data };
}

export function formatPaise(paise: number): string {
  const rupees = paise / 100;
  if (Math.abs(rupees) >= 100000) {
    return `₹${(rupees / 100000).toFixed(2)}L`;
  }
  return `₹${rupees.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function formatBps(bps: number): string {
  return `${(bps / 100).toFixed(1)}%`;
}
