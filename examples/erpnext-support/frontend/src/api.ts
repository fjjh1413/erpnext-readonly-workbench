// New workbench client. Credentials stay in the server-managed session cookie.
import type { AuditEvent, Case, Environment, ErrorState, ListQuery, PageResult, Role, Session, SystemStatus, Ticket, User } from './types'
let csrfToken = ''

export class ApiError extends Error {
  status: number
  requestId: string
  constructor(message: string, status = 0, requestId = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.requestId = requestId
  }
}

export function setCsrfToken(value: unknown) {
  csrfToken = typeof value === 'string' ? value : ''
}

export function toError(value: unknown): ErrorState {
  if (value instanceof ApiError) return value
  if (value instanceof Error) return { message: value.message }
  if (value && typeof value === 'object' && 'message' in value && typeof value.message === 'string') { const item = value as Record<string, unknown>; return { message: value.message, status: typeof item.status === 'number' ? item.status : undefined, requestId: typeof item.requestId === 'string' ? item.requestId : undefined } }
  return { message: '请求未完成，请重试。' }
}
export async function request<T = unknown>(path: string, { method = 'GET', data, signal }: { method?: string; data?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  if (!path.startsWith('/api/')) throw new ApiError('只允许访问当前工作台接口。')
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (data !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken
  let response: Response
  try {
    response = await fetch(path, {
      method, headers, credentials: 'same-origin', signal,
      body: data === undefined ? undefined : JSON.stringify(data),
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new ApiError('无法连接工作台，请检查本地服务后重试。')
  }
  const text = await response.text()
  let body: unknown = null
  let requestId = response.headers?.get('X-Request-ID') || ''
  if (text) {
    try { body = JSON.parse(text) } catch { throw new ApiError('服务返回了无法读取的结果，请重试。', response.status, requestId) }
  }
  if (!response.ok) {
    const failure = body && typeof body === 'object' ? body as Record<string, unknown> : {}
    if (typeof failure.request_id === 'string') requestId = failure.request_id
    const detail = typeof failure.detail === 'string' ? failure.detail : null
    if (response.status === 401 && path !== '/api/session') window.dispatchEvent(new CustomEvent<ErrorState>('session-expired', { detail: { message: '登录已失效，请重新登录。', status: 401, requestId } }))
    throw new ApiError(detail || (response.status === 403 ? '当前账号没有此操作的权限。' : '请求未完成，请重试。'), response.status, requestId)
  }
  return body as T
}

export function itemsOf<T>(value: unknown): T[] { if (Array.isArray(value)) return value as T[]; const body = value && typeof value === 'object' ? value as Record<string, unknown> : {}; return Array.isArray(body.items) ? body.items as T[] : [] }
export function pageOf<T>(value: unknown, limit = 50, offset = 0): PageResult<T> { const items = itemsOf<T>(value); const body = value && typeof value === 'object' ? value as Record<string, unknown> : {}; return { items, total: typeof body.total === 'number' ? body.total : items.length, limit: typeof body.limit === 'number' ? body.limit : limit, offset: typeof body.offset === 'number' ? body.offset : offset } }
export const itemPath = (resource: string, id: string) => `/api/${resource}/${encodeURIComponent(id)}`
function listPath(resource: string, query: ListQuery = {}): string { const params = new URLSearchParams(); for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value)); return `/api/${resource}${params.size ? '?' + params : ''}` }
export const api = {
  session: () => request<Session>('/api/session'),
  login: (data: { username: string; password: string }) => request<Session>('/api/session', { method: 'POST', data }),
  logout: () => request('/api/session', { method: 'DELETE' }),
  users: (query?: ListQuery) => request<PageResult<User>>(listPath('users', query)),
  environments: (query?: ListQuery) => request<PageResult<Environment>>(listPath('environments', query)),
  tickets: (query?: ListQuery) => request<PageResult<Ticket>>(listPath('tickets', query)),
  ticket: (id: string) => request<Ticket>(itemPath('tickets', id)),
  cases: (query?: ListQuery) => request<PageResult<Case>>(listPath('cases', query)),
  audit: (query?: ListQuery) => request<PageResult<AuditEvent>>(listPath('audit', query)),
  systemStatus: () => request<SystemStatus>('/api/system/status'),
  updateUser: (id: string, data: { role?: Role; environment_ids?: string[]; disabled?: boolean }) => request<User>(itemPath('users', id), { method: 'PATCH', data }),
  resetPassword: (id: string, password: string) => request(itemPath('users', id) + '/password', { method: 'POST', data: { password } }),
  create: <T = { id?: string }>(resource: string, data: unknown) => request<T>(`/api/${resource}`, { method: 'POST', data }),
  update: <T = unknown>(resource: string, id: string, data: unknown, method = 'PUT') => request<T>(itemPath(resource, id), { method, data }),
  remove: (resource: string, id: string) => request(itemPath(resource, id), { method: 'DELETE' }),
  action: <T = unknown>(resource: string, id: string, action: string, data: unknown = {}) => request<T>(`${itemPath(resource, id)}/${action}`, { method: 'POST', data }),
}
