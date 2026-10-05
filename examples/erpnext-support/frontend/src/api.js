// New workbench client. Credentials stay in the server-managed session cookie.
let csrfToken = ''

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function setCsrfToken(value) {
  csrfToken = typeof value === 'string' ? value : ''
}

export async function request(path, { method = 'GET', data, signal } = {}) {
  if (!path.startsWith('/api/')) throw new ApiError('只允许访问当前工作台接口。')
  const headers = { Accept: 'application/json' }
  if (data !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken
  let response
  try {
    response = await fetch(path, {
      method, headers, credentials: 'same-origin', signal,
      body: data === undefined ? undefined : JSON.stringify(data),
    })
  } catch (error) {
    if (error.name === 'AbortError') throw error
    throw new ApiError('无法连接工作台，请检查本地服务后重试。')
  }
  const text = await response.text()
  let body = null
  if (text) {
    try { body = JSON.parse(text) } catch { throw new ApiError('服务返回了无法读取的结果，请重试。', response.status) }
  }
  if (!response.ok) {
    const detail = typeof body?.detail === 'string' ? body.detail : null
    if (response.status === 401 && path !== '/api/session') window.dispatchEvent(new Event('session-expired'))
    throw new ApiError(detail || (response.status === 403 ? '当前账号没有此操作的权限。' : '请求未完成，请重试。'), response.status)
  }
  return body
}

export const itemsOf = (value) => Array.isArray(value?.items) ? value.items : []
export const itemPath = (resource, id) => `/api/${resource}/${encodeURIComponent(id)}`
export const api = {
  session: () => request('/api/session'),
  login: (data) => request('/api/session', { method: 'POST', data }),
  logout: () => request('/api/session', { method: 'DELETE' }),
  users: () => request('/api/users'),
  environments: () => request('/api/environments'),
  tickets: () => request('/api/tickets'),
  ticket: (id) => request(itemPath('tickets', id)),
  cases: () => request('/api/cases'),
  create: (resource, data) => request(`/api/${resource}`, { method: 'POST', data }),
  update: (resource, id, data, method = 'PUT') => request(itemPath(resource, id), { method, data }),
  remove: (resource, id) => request(itemPath(resource, id), { method: 'DELETE' }),
  action: (resource, id, action, data = {}) => request(`${itemPath(resource, id)}/${action}`, { method: 'POST', data }),
}
