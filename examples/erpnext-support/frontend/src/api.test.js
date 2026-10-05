import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, itemPath, itemsOf, request, setCsrfToken } from './api.js'

// These tests mock HTTP explicitly. They do not claim ERP or backend connectivity.
const response = (body, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) })
beforeEach(() => { setCsrfToken(''); vi.stubGlobal('fetch', vi.fn()) })
afterEach(() => { vi.unstubAllGlobals(); setCsrfToken('') })
describe('same-origin API client', () => {
  it('sends session cookies and CSRF on mutations', async () => { setCsrfToken('test-csrf'); fetch.mockResolvedValue(response({ id: 't1' })); await api.create('tickets', { delivery_note: 'DN-1' }); expect(fetch).toHaveBeenCalledWith('/api/tickets', expect.objectContaining({ credentials: 'same-origin', method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }), body: '{"delivery_note":"DN-1"}' })) })
  it('does not put the CSRF token on read requests', async () => { setCsrfToken('token'); fetch.mockResolvedValue(response({ items: [] })); await api.tickets(); expect(fetch.mock.calls[0][1].headers['X-CSRF-Token']).toBeUndefined() })
  it('rejects external URLs before any fetch', async () => { await expect(request('https://external.invalid/api/tickets')).rejects.toBeInstanceOf(ApiError); expect(fetch).not.toHaveBeenCalled() })
  it('retains 403 status and server explanation', async () => { fetch.mockResolvedValue(response({ detail: '此环境未授权' }, 403)); await expect(api.environments()).rejects.toMatchObject({ status: 403, message: '此环境未授权' }) })
  it('signals expiry when a protected resource returns 401', async () => { const listener = vi.fn(); window.addEventListener('session-expired', listener); fetch.mockResolvedValue(response({ detail: '请登录' }, 401)); await expect(api.tickets()).rejects.toMatchObject({ status: 401 }); expect(listener).toHaveBeenCalledOnce(); window.removeEventListener('session-expired', listener) })
  it('shows connection errors without substituting sample data', async () => { fetch.mockRejectedValue(new TypeError('network')); await expect(api.cases()).rejects.toMatchObject({ status: 0, message: '无法连接工作台，请检查本地服务后重试。' }); expect(itemsOf(null)).toEqual([]) })
  it('reports malformed responses', async () => { fetch.mockResolvedValue({ ok: true, status: 200, text: async () => '<html>' }); await expect(api.tickets()).rejects.toMatchObject({ message: '服务返回了无法读取的结果，请重试。' }) })
  it('encodes item IDs so they cannot become new path segments', () => { expect(itemPath('tickets', 'a/b?#')).toBe('/api/tickets/a%2Fb%3F%23') })
})
