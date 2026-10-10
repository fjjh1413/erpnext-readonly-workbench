import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { api } from '../api'
import DetailPage from './DetailPage.vue'
vi.mock('../api', async importOriginal => ({ ...(await importOriginal()), api: { ticket: vi.fn(), action: vi.fn() } }))
const wrappers = []
const record = (state = 'partial') => ({ id: 't1', delivery_note: 'DN-真实测试', environment_id: 'env1', status: 'needs_information', description: '提交出现错误', tasks: [{ id: 'task1', kind: 'diagnose', state, steps: [], elapsed_seconds: 1.2 }], actions: [], latest_result: { summary: '证据不完整', confirmed_facts: [{ statement: '已读取单据', evidence_ids: ['e1'] }], supporting_evidence: [{ id: 'e1', source: 'Delivery Note', snippet: '<script>danger</script>', collected_at: '2026-10-05T10:00:00Z' }], missing_information: ['无故障用户的提交权限'], candidate_causes: [] } })
function create(user = { role: 'analyst' }) { const wrapper = mount(DetailPage, { props: { user, ticketId: 't1' }, global: { plugins: [ElementPlus], stubs: { ElDescriptions: { template: '<div><slot /></div>' }, ElDescriptionsItem: { template: '<div><slot /></div>' } } } }); wrappers.push(wrapper); return wrapper }
beforeEach(() => { vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} }); api.ticket.mockResolvedValue(record()); api.action.mockResolvedValue({ task: { id: 'created' } }) })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.clearAllMocks(); vi.unstubAllGlobals() })
describe('diagnostic detail using explicitly mocked HTTP', () => {
  it('shows partial completion, evidence and uncertainty without executing evidence HTML', async () => { const wrapper = create(); await flushPromises(); expect(wrapper.text()).toContain('诊断部分完成'); expect(wrapper.text()).toContain('无故障用户的提交权限'); expect(wrapper.text()).toContain('证据不足，尚未形成候选原因'); expect(wrapper.findAll('script')).toHaveLength(0); expect(wrapper.html()).toContain('&lt;script&gt;danger&lt;/script&gt;') })
  it('marks interrupted tasks clearly while preserving prior evidence', async () => { api.ticket.mockResolvedValue(record('interrupted')); const wrapper = create(); await flushPromises(); expect(wrapper.text()).toContain('诊断已中断'); expect(wrapper.text()).toContain('已有证据保留'); expect(wrapper.text()).toContain('已读取单据') })
  it('does not let an analyst close a ticket', async () => { const value = record('completed'); value.tasks.unshift({ id: 'review1', kind: 'review', state: 'completed', result: { review: { business_complete: true } } }); api.ticket.mockResolvedValue(value); const wrapper = create(); await flushPromises(); const button = wrapper.findAll('button').find(item => item.text() === '人工确认关闭'); expect(button.attributes('disabled')).toBeDefined(); expect(api.action).not.toHaveBeenCalled() })
  it('renders permission errors as errors rather than diagnostic data', async () => { api.ticket.mockRejectedValue({ status: 403, message: '此工单所属环境未授权' }); const wrapper = create(); await flushPromises(); expect(wrapper.text()).toContain('权限不足'); expect(wrapper.text()).toContain('此工单所属环境未授权'); expect(wrapper.text()).not.toContain('已读取单据') })
  it('creates a real API task request from the start control', async () => { const wrapper = create(); await flushPromises(); const button = wrapper.findAll('button').find(item => item.text() === '重新诊断'); await button.trigger('click'); await flushPromises(); expect(api.action).toHaveBeenCalledWith('tickets', 't1', 'diagnose', {}) })
})
