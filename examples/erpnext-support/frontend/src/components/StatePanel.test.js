import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import StatePanel from './StatePanel.vue'
const create = (props) => mount(StatePanel, { props, slots: { default: '<p>实际业务数据</p>' }, global: { plugins: [ElementPlus] } })
describe('data state presentation', () => {
  it('shows loading before any business data', () => { const wrapper = create({ loading: true }); expect(wrapper.text()).toContain('正在读取实际数据'); expect(wrapper.text()).not.toContain('实际业务数据'); wrapper.unmount() })
  it('distinguishes 403 from an empty result and emits retry', async () => { const wrapper = create({ error: { status: 403, message: '未授权 ERP 环境' } }); expect(wrapper.text()).toContain('权限不足'); expect(wrapper.text()).toContain('未授权 ERP 环境'); expect(wrapper.text()).not.toContain('实际业务数据'); await wrapper.find('button').trigger('click'); expect(wrapper.emitted('retry')).toHaveLength(1); wrapper.unmount() })
  it('shows empty text only for an actual empty state', () => { const wrapper = create({ empty: true, emptyText: '没有可访问工单' }); expect(wrapper.text()).toContain('没有可访问工单'); expect(wrapper.text()).not.toContain('实际业务数据'); wrapper.unmount() })
  it('renders the returned business slot', () => { const wrapper = create({}); expect(wrapper.text()).toContain('实际业务数据'); wrapper.unmount() })
})
