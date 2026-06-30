/**
 * Tiny typed fetch wrapper — the single choke point for talking to the
 * Nightshift `/api/*` backend. Mirrors the shape of Longitude's api.ts (get /
 * post / put / patch / del) so the two repos share a mental model.
 *
 * All paths are relative to `/api`; in dev Vite proxies that to the right
 * backend (manager :8800 or worker :8810), in prod it's same-origin under the
 * static mount. No base-URL config needed.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly url: string,
    readonly body: string,
  ) {
    super(`${status} ${url}: ${body.slice(0, 200)}`)
    this.name = 'ApiError'
  }
}

const BASE = '/api'

/** Append a `?queue=` param when a non-main queue is in scope. null/undefined → main. */
export function withQueue(path: string, queue?: string | null): string {
  if (!queue) return path
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}queue=${encodeURIComponent(queue)}`
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const url = `${BASE}${path}`
  const init: RequestInit = {
    method,
    headers: { Accept: 'application/json' },
  }
  if (body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }

  const res = await fetch(url, init)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new ApiError(res.status, url, text)
  }

  // 204 / empty body → undefined (caller types it as void).
  if (res.status === 204) return undefined as T
  const text = await res.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
