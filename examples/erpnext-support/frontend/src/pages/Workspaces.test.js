import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElFormItem, ElInput } from 'element-plus'
import { api } from '../api'
import TicketsPage from './TicketsPage.vue'
import CasesPage from './CasesPage.vue'
import EnvironmentsPage from './EnvironmentsPage.vue'

// HTTP is mocked only in these component tests. No fabricated data ships in the UI.
vi.mock('../api', async importOriginal => ({ ...(await importOriginal()), itemsOf: value => Array.isArray(value?.items) ? value.items : [], api: { tickets: vi.fn(), environments: vi.fn(), users: vi.fn(), cases: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), action: vi.fn() } }))
const wrappers = []
function create(component, role = 'admin') {
  const wrapper = mount(component, {
    props: { user: { id: 'u1', username: 'qa', role } },
    global: { plugins: [ElementPlus], stubs: {
      teleport: true,
      // Popup geometry belongs to the real browser E2E; unit tests retain v-model/change.
      ElSelect: { props: ['modelValue', 'disabled'], emits: ['update:modelValue', 'change'], template: '<select :value="modelValue" :disabled="disabled" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\', $event.target.value)"><slot /></select>' },
      ElOption: { props: ['value', 'label'], template: '<option :value="value">{{ label }}</option>' },
      ElDescriptions: { template: '<div><slot /></div>' }, ElDescriptionsItem: { template: '<div><slot /></div>' },
    } },
  })
  wrappers.push(wrapper)
  return wrapper
}
function button(wrapper, text) { return wrapper.findAll('button').find(item => item.text() === text) }
async function field(wrapper, label, value) { const formItem = wrapper.findAllComponents(ElFormItem).find(item => item.text().includes(label)); const input = formItem.findComponent(ElInput); input.vm.$emit('update:modelValue', value); await flushPromises() }
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  api.tickets.mockResolvedValue({ items: [] }); api.environments.mockResolvedValue({ items: [{ id: 'env1', name: '隔离测试环境' }] }); api.users.mockResolvedValue({ items: [{ id: 'u1', username: 'qa' }] }); api.cases.mockResolvedValue({ items: [] }); api.create.mockResolvedValue({ id: 'new1' }); api.update.mockResolvedValue({}); api.action.mockResolvedValue({ capabilities: { payload: { version_supported: false, read_permissions: { 'Delivery Note': false } } }, missing_information: ['数据源权限不足'] })
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.clearAllMocks(); vi.unstubAllGlobals() })
describe('three list workspaces with mocked HTTP', () => {
  it('shows actual ticket rows and supports status filtering', async () => {
    api.tickets.mockResolvedValue({ items: [{ id: 't1', delivery_note: 'DN-待诊断', environment_id: 'env1', description: '提交报错', status: 'pending' }, { id: 't2', delivery_note: 'DN-已关闭', environment_id: 'env1', description: '已经复核', status: 'closed' }] })
    const wrapper = create(TicketsPage); await flushPromises(); expect(wrapper.text()).toContain('DN-待诊断'); expect(wrapper.text()).toContain('DN-已关闭'); await wrapper.find('select[aria-label="状态筛选"]').setValue('closed'); await flushPromises(); expect(wrapper.text()).not.toContain('DN-待诊断'); expect(wrapper.text()).toContain('DN-已关闭')
  })
  it('shows empty tickets rather than example diagnoses', async () => { const wrapper = create(TicketsPage); await flushPromises(); expect(wrapper.text()).toContain('还没有工单'); expect(api.tickets).toHaveBeenCalledOnce(); expect(api.users).toHaveBeenCalledOnce() })
  it('shows case approval controls only to reviewers', async () => { api.cases.mockResolvedValue({ items: [{ id: 'c1', title: '权限核对案例', symptom: '未获得提交权限', status: 'draft', conditions: [] }] }); const wrapper = create(CasesPage, 'analyst'); await flushPromises(); expect(wrapper.text()).toContain('权限核对案例'); expect(button(wrapper, '审核案例')).toBeUndefined(); expect(wrapper.text()).toContain('当前未获审核通过') })
  it('creates a case using the edited fields and array contract', async () => { const wrapper = create(CasesPage); await flushPromises(); await button(wrapper, '创建案例').trigger('click'); await flushPromises(); await field(wrapper, '案例标题', '测试权限案例'); await field(wrapper, '适用 ERPNext', '15.85.1'); await field(wrapper, '适用 Frappe', '15.87.0'); await field(wrapper, '问题现象', '发货单提交被权限拒绝'); await field(wrapper, '适用条件', '只读调查\n当前固定版本'); await field(wrapper, '证据要求', '原始错误\n操作用户'); await field(wrapper, '人工处理步骤', '由授权管理员核对角色'); await field(wrapper, '复核方法', '读取业务状态并人工确认'); await field(wrapper, '来源链接', 'https://docs.frappe.io/erpnext/user/manual/en/delivery-note'); await button(wrapper, '保存并待审核').trigger('click'); await flushPromises(); expect(api.create).toHaveBeenCalledWith('cases', expect.objectContaining({ title: '测试权限案例', conditions: ['只读调查', '当前固定版本'], evidence_requirements: ['原始错误', '操作用户'] })) })
  it('makes configuration read only for analysts', async () => { const wrapper = create(EnvironmentsPage, 'analyst'); await flushPromises(); expect(button(wrapper, '新增环境').attributes('disabled')).toBeDefined(); expect(button(wrapper, '编辑').attributes('disabled')).toBeDefined(); expect(wrapper.text()).toContain('配置变更需要管理员') })
  it('shows returned capability failures without relabeling them as success', async () => { const wrapper = create(EnvironmentsPage); await flushPromises(); await button(wrapper, '连接与能力检查').trigger('click'); await flushPromises(); expect(api.action).toHaveBeenCalledWith('environments', 'env1', 'check'); expect(wrapper.text()).toContain('数据源权限不足'); expect(wrapper.text()).toContain('version_supported'); expect(wrapper.text()).toContain('false'); expect(wrapper.text()).not.toContain('连接成功') })
  it('retains an environment check 403 separately from environment configuration', async () => { api.action.mockRejectedValue({ status: 403, message: '环境权限已撤销' }); const wrapper = create(EnvironmentsPage); await flushPromises(); await button(wrapper, '连接与能力检查').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('权限不足：环境权限已撤销'); expect(wrapper.text()).toContain('隔离测试环境') })
  it('edits approved cases without sending server metadata as forbidden request fields', async () => {
    api.cases.mockResolvedValue({ items: [{ id: 'c1', title: '真实证据案例', symptom: '当前样例已提交', status: 'approved', created_by: 'qa', created_at: '2026-10-05', approval_note: '已审核', erpnext_version: '15.85.1', frappe_version: '15.87.0', conditions: ['仅隔离样例'], evidence_requirements: ['原始单据'], steps: ['人工核对'], verification: ['重新采证'], source_url: 'https://github.com/frappe/erpnext' }] })
    const wrapper = create(CasesPage); await flushPromises(); await button(wrapper, '编辑').trigger('click'); await flushPromises(); await button(wrapper, '保存并待审核').trigger('click'); await flushPromises(); const data = api.update.mock.calls[0][2]; expect(data.title).toBe('真实证据案例'); expect(data.status).toBeUndefined(); expect(data.created_by).toBeUndefined(); expect(data.approval_note).toBeUndefined(); expect(data.id).toBeUndefined()
  })
  it('edits environments with collector aliases while omitting persisted check metadata', async () => {
    api.environments.mockResolvedValue({ items: [{ id: 'env1', name: '隔离环境', base_url: 'http://127.0.0.1:8090', credential_ref: 'erp-lab', collector_ref: 'erp-lab', erpnext_version: '15.85.1', frappe_version: '15.87.0', installed_apps: ['frappe'], allowed_services: ['database'], log_source: 'none', model_consent: false, capability_check: { version_supported: true }, created_at: '2026-10-05' }] })
    const wrapper = create(EnvironmentsPage); await flushPromises(); await button(wrapper, '编辑').trigger('click'); await flushPromises(); await button(wrapper, '保存配置').trigger('click'); await flushPromises(); const data = api.update.mock.calls[0][2]; expect(data.collector_ref).toBe('erp-lab'); expect(data.allowed_services).toEqual(['database']); expect(data.capability_check).toBeUndefined(); expect(data.created_at).toBeUndefined(); expect(data.id).toBeUndefined()
  })
})
