import type { PageName, User } from './types'
export const statuses: Record<string, string> = {
  pending: '待诊断', investigating: '调查中', awaiting_action: '待人工处理',
  needs_information: '待补充信息', awaiting_review: '待复核', closed: '已关闭',
  queued: '排队中', running: '执行中', completed: '已完成', partial: '部分完成',
  failed: '执行失败', interrupted: '已中断', draft: '待审核', approved: '已审核', rejected: '已退回',
  admin: '管理员', analyst: '售后分析员', reviewer: '审核员',
  success: '调用成功', error: '调用失败', confirmed: '已确认', candidate: '候选',
  rules_degraded: '规则降级模式（未完成模型分析）', holmes_agent: 'Holmes Agent',
  stock_shortage: '当前库存不足', historical_stock_shortage: '历史时点库存不足',
  document_precondition: '关联单据前置条件', document_state: '单据状态异常',
  reservation_warehouse: '预留或仓库不匹配', batch_metadata_conflict: '批次条件冲突',
  batch_bundle_conflict: '批次组合不匹配', batch_expired_now: '批次当前已过期',
  diagnostic_permission: '诊断权限不足', runtime_unavailable: '运行环境不可用',
  unable_to_confirm: '无法确认原因', normal: '未发现规则故障',
}

export const statusLabel = (value?: string) => value ? statuses[value] || value : '未提供'
export function statusType(value?: string) {
  if (!value) return 'info'
  if (['success', 'confirmed', 'closed', 'completed', 'approved', 'healthy', 'available', 'supported'].includes(value)) return 'success'
  if (['error', 'failed', 'rejected', 'interrupted', 'unavailable', 'unsupported', 'denied'].includes(value)) return 'danger'
  if (['partial', 'awaiting_action', 'needs_information', 'awaiting_review', 'degraded'].includes(value)) return 'warning'
  return 'info'
}

export function formatTime(value?: string | number | Date | null) {
  if (!value) return '未提供'
  const date = new Date(value instanceof Date ? value.getTime() : value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
}

export const asLines = (text: unknown) => String(text || '').split('\n').map((line) => line.trim()).filter(Boolean)
export const asText = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? ''

export function parseRoute(hash: string): { page: PageName; id: string } {
  const path = String(hash || '#/tickets').replace(/^#/, '')
  if (path.startsWith('/tickets/')) { try { return { page: 'detail', id: decodeURIComponent(path.slice(9)) } } catch { return { page: 'tickets', id: '' } } }
  if (['/tickets', '/cases', '/environments', '/detail', '/users', '/audit'].includes(path)) return { page: path.slice(1) as PageName, id: '' }
  return { page: 'tickets', id: '' }
}

export function canReview(user?: User | null) { return !!user && ['admin', 'reviewer'].includes(user.role) }
const limitations: Record<string, string> = {
  operator_submit_permission_unknown: '尚未核验故障操作用户的提交权限',
  runtime_health_not_available: 'ERP API 未提供运行环境健康证据；如已配置独立采集器，请对照其结果',
  inventory_dimensions_not_adapted: '当前库存维度尚未适配',
  custom_workflow_not_adapted: '当前自定义工作流尚未适配',
  stock_reposting_in_progress: '库存重算正在进行，数量证据需要复核',
  additional_apps_not_adapted: '检测到尚未适配的额外应用',
  batch_or_serial_stock_not_adapted: '批次或序列号的库存规则尚未适配',
  document_changed_during_collection: '采集期间单据已变化，证据时点不一致',
}
export const limitationText = (value: string) => limitations[value] ? `${limitations[value]}（${value}）` : value
