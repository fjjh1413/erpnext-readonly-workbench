<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, toError } from '../api'
import type { ErrorState, Ticket, User } from '../types'
import { asText, canReview, formatTime, statusLabel } from '../labels'
import { evidenceId, findingText, latestReview, referencesOf, resultView, reviewPassed, taskIsActive } from '../result'
import StatePanel from '../components/StatePanel.vue'
import StatusTag from '../components/StatusTag.vue'
import ErrorNotice from '../components/ErrorNotice.vue'

const props = defineProps<{ user: User; ticketId?: string }>()
defineEmits(['back'])
const loading = ref(false), error = ref<ErrorState | null>(null), ticket = ref<Ticket | null>(null), acting = ref(''), actionError = ref<ErrorState | null>(null)
const expanded = ref<string[]>([]), expandedTasks = ref<string[]>([]), recordDescription = ref(''), closeDialog = ref(false)
const closeForm = reactive({ confirmation: '', manual_override: false })
const tasks = computed(() => Array.isArray(ticket.value?.tasks) ? ticket.value.tasks : [])
const active = computed(() => tasks.value.some(taskIsActive))
const newestTask = computed(() => tasks.value[0] || null)
const newestReview = computed(() => latestReview(tasks.value))
const view = computed(() => resultView(ticket.value?.latest_result))
const closed = computed(() => ticket.value?.status === 'closed')
const reviewer = computed(() => canReview(props.user))
let timer: ReturnType<typeof setTimeout> | null = null, requestVersion = 0, disposed = false
function stopPolling() { if (timer) clearTimeout(timer); timer = null }
function pollLater() {
  stopPolling()
  if (active.value && !disposed) timer = setTimeout(() => load(true), 2000)
}
async function load(quiet = false) {
  if (!props.ticketId) return
  const version = ++requestVersion
  if (!quiet) loading.value = true
  error.value = null
  try {
    const value = await api.ticket(props.ticketId)
    if (version !== requestVersion || disposed) return
    ticket.value = value
    pollLater()
  } catch (failure) {
    if (version !== requestVersion || disposed) return
    ticket.value = null; error.value = toError(failure); stopPolling()
  } finally { if (version === requestVersion) loading.value = false }
}
async function operate(name: string, body: unknown = {}) {
  if (!props.ticketId) return
  acting.value = name; actionError.value = null
  try {
    await api.action('tickets', props.ticketId, name, body)
    if (name === 'actions') recordDescription.value = ''
    if (name === 'close') closeDialog.value = false
    await load(true)
    ElMessage.success(name === 'close' ? '工单已由人工确认关闭。' : name === 'actions' ? '处理记录已保存。' : '任务已创建，请查看执行进度。')
  } catch (failure) { actionError.value = toError(failure) }
  finally { acting.value = '' }
}
function saveRecord() {
  if (recordDescription.value.trim().length < 5) { actionError.value = { message: '请至少用 5 个字记录实际人工处理动作。' }; return }
  operate('actions', { description: recordDescription.value.trim() })
}
function confirmClose() {
  const minimum = closeForm.manual_override ? 20 : 5
  if (closeForm.confirmation.trim().length < minimum) { actionError.value = { message: `关闭说明至少需要 ${minimum} 个字。` }; return }
  if (!newestReview.value) { actionError.value = { message: '请先执行业务复核，再由审核人员确认关闭。' }; return }
  if (!reviewPassed(newestReview.value) && !closeForm.manual_override) { actionError.value = { message: '复核尚未完整通过；需要明确说明人工复核理由。' }; return }
  operate('close', { ...closeForm, confirmation: closeForm.confirmation.trim(), review_id: newestReview.value.id })
}
function openEvidence(id: string) {
  if (!expanded.value.includes(id)) expanded.value = [...expanded.value, id]
  document.getElementById('evidence-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
watch(() => props.ticketId, () => { stopPolling(); requestVersion++; ticket.value = null; actionError.value = null; expanded.value = []; load() }, { immediate: true })
watch(() => newestTask.value?.id, (id) => { if (id && !expandedTasks.value.includes(id)) expandedTasks.value = [...expandedTasks.value, id] })
onBeforeUnmount(() => { disposed = true; requestVersion++; stopPolling() })
</script>

<template>
  <section>
    <div class="page-heading"><div><el-button link @click="$emit('back')">← 返回工单工作台</el-button><h1>诊断详情</h1><p>查看实际调查步骤和证据，记录人工处理，再核验业务状态。</p></div><el-button v-if="ticketId" :loading="loading" @click="load()">刷新详情</el-button></div>
    <el-empty v-if="!ticketId" description="请先从工单工作台选择一张工单"><el-button type="primary" @click="$emit('back')">选择工单</el-button></el-empty>
    <StatePanel v-else :loading="loading" :error="error" :empty="!ticket" empty-text="未取得工单详情" @retry="load()">
      <template v-if="ticket">
        <div class="panel ticket-overview"><div class="ticket-title"><div><span class="eyebrow">DELIVERY NOTE</span><h2>{{ ticket.delivery_note }}</h2></div><StatusTag :value="ticket.status" /></div><p>{{ ticket.description }}</p>
          <el-descriptions :column="3" border><el-descriptions-item label="环境">{{ ticket.environment_name || ticket.environment_id }}</el-descriptions-item><el-descriptions-item label="负责人">{{ ticket.owner || '未分配' }}</el-descriptions-item><el-descriptions-item label="提交人">{{ ticket.submitter }}</el-descriptions-item><el-descriptions-item label="发生时间">{{ formatTime(ticket.occurred_at) }}</el-descriptions-item><el-descriptions-item label="故障操作用户">{{ ticket.operator_user || '未提供；不能判断其提交权限' }}</el-descriptions-item><el-descriptions-item label="工单编号">{{ ticket.id }}</el-descriptions-item></el-descriptions>
          <div class="action-row"><el-button type="primary" :loading="acting === 'diagnose'" :disabled="active || closed || !!acting" @click="operate('diagnose')">{{ tasks.length ? '重新诊断' : '开始诊断' }}</el-button><el-button :loading="acting === 'review'" :disabled="active || closed || !!acting" @click="operate('review')">读取业务状态并复核</el-button><el-button :disabled="!reviewer || active || closed || !newestReview" @click="closeDialog = true; closeForm.confirmation = ''; closeForm.manual_override = false">人工确认关闭</el-button><small class="muted">关闭仅限审核员和管理员；所有处理建议均需人工执行。</small></div>
        </div>
        <ErrorNotice :error="actionError" />
        <el-alert v-if="newestTask?.state === 'interrupted'" title="诊断已中断" description="上次任务未完成，已有证据保留。确认环境可用后可重新执行，不能把中断结果当作完整诊断。" type="error" :closable="false" show-icon class="space-bottom" />
        <el-alert v-else-if="newestTask?.state === 'partial'" title="诊断部分完成" description="部分数据源或执行步骤未取得完整证据，请查看缺失信息和失败记录。" type="warning" :closable="false" show-icon class="space-bottom" />
        <el-alert v-else-if="newestTask?.state === 'failed'" title="诊断执行失败" :description="newestTask.error || '请核对环境、权限和数据源后重试。'" type="error" :closable="false" show-icon class="space-bottom" />
        <el-alert v-if="active" title="调查任务正在执行" description="详情每 2 秒自动刷新。退出页面不会取消持久化任务。" type="info" :closable="false" show-icon class="space-bottom" />
        <div class="detail-grid">
          <div class="panel"><h2>步骤与任务记录</h2><el-empty v-if="!tasks.length" description="尚未执行诊断任务" />
            <el-collapse v-else v-model="expandedTasks"><el-collapse-item v-for="task in tasks" :key="task.id" :name="task.id"><template #title><div class="task-title"><span>{{ task.kind === 'review' ? '业务复核' : '专项诊断' }} · {{ task.id }}</span><StatusTag :value="task.state" /></div></template>
              <p class="muted">{{ formatTime(task.created_at || task.started_at) }} · 耗时 {{ task.elapsed_seconds == null ? '尚未记录' : Number(task.elapsed_seconds).toFixed(3) + ' 秒' }}</p><el-alert v-if="task.error" :title="task.error" type="error" :closable="false" /><el-alert v-if="task.evidence_expired" title="原证据已按保留期限过期" description="原工具记录不能继续查看，需要重新采证后再判断。" type="warning" :closable="false" />
              <el-timeline class="space-top"><el-timeline-item v-for="(step, index) in task.steps || []" :key="step.id || index" :timestamp="step.elapsed_seconds == null ? undefined : Number(step.elapsed_seconds).toFixed(3) + ' 秒'" placement="top"><strong>{{ step.title || step.name || step.tool_name || '步骤 ' + (index + 1) }}</strong> <StatusTag :value="step.state || step.status" /><p v-if="step.description || step.error">{{ step.description || step.error }}</p><p v-if="step.tool_call_id" class="muted">{{ step.evidence_id }} · {{ step.tool_call_id }} · {{ formatTime(step.collected_at) }}</p><el-collapse v-if="step.tool_result"><el-collapse-item title="查看本步实际工具记录" :name="step.tool_call_id || index"><pre class="evidence-content">{{ asText(step.tool_result) }}</pre></el-collapse-item></el-collapse></el-timeline-item></el-timeline><p v-if="!task.steps?.length" class="muted">当前任务尚无已记录步骤。</p>
              <div v-if="task.kind === 'review' && task.result" class="review-result"><h3>业务复核结果</h3><pre>{{ asText(task.result) }}</pre><p class="muted">接口可用或单据状态变化，不会自动证明先前候选原因正确。最终关闭由人工确认。</p></div>
            </el-collapse-item></el-collapse>
          </div>
          <div class="panel"><h2>诊断结论</h2><el-empty v-if="!ticket.latest_result" description="尚无实际诊断结果" /><template v-else>
            <div class="result-meta"><el-tag v-if="view.category">{{ statusLabel(view.category) }}</el-tag><small class="muted" v-if="view.mode">执行模式：{{ statusLabel(view.mode) }}</small><small v-if="view.model" class="muted">模型：{{ view.model }}</small></div><p class="result-summary">{{ view.summary || '请对照以下实际事实、候选原因与引用证据判断。' }}</p><p v-if="view.metrics.tool_calls != null" class="muted">已调用 {{ view.metrics.tool_calls }} 次 · 调用成功率 {{ view.metrics.tool_success_rate == null ? '未记录' : (view.metrics.tool_success_rate * 100).toFixed(1) + '%' }} · 总耗时 {{ view.metrics.elapsed_seconds == null ? '未记录' : Number(view.metrics.elapsed_seconds).toFixed(3) + ' 秒' }}</p><p v-if="view.modelTarget" class="muted">模型目标：{{ view.modelTarget }}</p>
            <h3>已确认事实</h3><p v-if="!view.facts.length" class="muted">暂无已确认事实。</p><div v-for="(fact, index) in view.facts" :key="index" class="finding"><p>{{ findingText(fact) }}</p><el-tag v-for="id in referencesOf(fact)" :key="id" size="small" class="evidence-ref" @click="openEvidence(id)">{{ id }}</el-tag></div>
            <h3>候选原因</h3><p v-if="!view.causes.length" class="muted">证据不足，尚未形成候选原因。</p><div v-for="(cause, index) in view.causes" :key="index" class="finding"><el-tag v-if="cause.model_hypothesis" size="small" type="warning">模型候选 · 尚需确认</el-tag><StatusTag v-else-if="cause.status" :value="cause.status" /><p>{{ findingText(cause) }}</p><el-tag v-for="id in referencesOf(cause)" :key="id" size="small" class="evidence-ref" @click="openEvidence(id)">{{ id }}</el-tag></div>
            <h3>缺失信息与范围限制</h3><ul class="plain-list"><li v-for="(item, index) in [...view.missing, ...view.limits]" :key="index">{{ findingText(item) }}</li></ul><p v-if="!view.missing.length && !view.limits.length" class="muted">当前结果未列出缺失信息；仍需人员核对诊断范围。</p>
            <h3>人工处理建议</h3><ol class="plain-list"><li v-for="(item, index) in view.next" :key="index">{{ findingText(item) }}</li></ol><p v-if="!view.next.length" class="muted">尚无处理建议，请补充证据。</p><h3 v-if="view.verification.length">建议复核方式</h3><ul class="plain-list"><li v-for="(item, index) in view.verification" :key="index">{{ findingText(item) }}</li></ul>
          </template></div>
        </div>
        <div id="evidence-panel" class="panel space-top"><h2>证据与工具调用</h2><p class="muted">内容来自实际只读查询，按原始文本展示；请核对编号、采集时间与业务时点。</p><el-empty v-if="!view.evidence.length" description="当前没有可展开的业务或运行证据" /><el-collapse v-else v-model="expanded"><el-collapse-item v-for="(item, index) in view.evidence" :key="evidenceId(item, index)" :name="evidenceId(item, index)"><template #title><span class="evidence-title">{{ evidenceId(item, index) }} · {{ item.source || item.tool_name || '未提供来源' }}</span><StatusTag v-if="item.status" :value="item.status" /></template><el-descriptions :column="2" border><el-descriptions-item label="采集时间">{{ formatTime(item.collected_at || item.observed_at || item.acquired_at) }}</el-descriptions-item><el-descriptions-item label="业务时点">{{ formatTime(item.business_at || item.business_time || item.occurred_at) }}</el-descriptions-item><el-descriptions-item label="查询条件" :span="2"><pre>{{ asText(item.query || item.conditions || item.params || {}) }}</pre></el-descriptions-item></el-descriptions><pre class="evidence-content">{{ asText(item.snippet ?? item.payload ?? item.observation ?? item.data ?? item.result ?? item) }}</pre><ul v-if="item.limitations?.length" class="plain-list"><li v-for="(line, lineIndex) in item.limitations" :key="lineIndex">{{ line }}</li></ul></el-collapse-item></el-collapse>
          <el-collapse v-if="view.calls.length" class="space-top"><el-collapse-item title="查看保留的工具调用记录" name="calls"><el-table :data="view.calls" size="small"><el-table-column prop="tool_call_id" label="调用编号" min-width="220" /><el-table-column prop="tool_name" label="工具" min-width="180" /><el-table-column label="执行状态" width="140"><template #default="{ row }"><StatusTag :value="row.result?.status" /></template></el-table-column><el-table-column label="耗时" width="120"><template #default="{ row }">{{ row.result?.elapsed_seconds == null ? '未记录' : Number(row.result.elapsed_seconds).toFixed(3) + ' 秒' }}</template></el-table-column></el-table></el-collapse-item></el-collapse>
        </div>
        <div class="detail-grid space-top"><div class="panel"><h2>人工处理记录</h2><p class="muted">只记录已实际执行的动作，不会向 ERP 发起修改。请不要填写密码或 Token。</p><el-input v-model="recordDescription" type="textarea" :rows="4" maxlength="3000" show-word-limit :disabled="closed" placeholder="记录处理人、实际动作、执行时间及结果" /><el-button class="space-top" type="primary" :loading="acting === 'actions'" :disabled="closed || active || !!acting" @click="saveRecord">保存处理记录</el-button><el-timeline class="space-top"><el-timeline-item v-for="(item, index) in ticket.actions || []" :key="item.id || index" :timestamp="formatTime(item.created_at)"><strong>{{ item.actor || item.username || item.created_by }}</strong><p>{{ item.description }}</p></el-timeline-item></el-timeline><p v-if="!ticket.actions?.length" class="muted">暂无人工处理记录。</p></div><div class="panel"><h2>复核与关闭</h2><p>记录人工处理后，重新读取发货单和库存相关业务状态。工单最终关闭须由审核人员确认。</p><el-alert v-if="newestReview" :title="'最近复核：' + newestReview.id" :type="reviewPassed(newestReview) ? 'success' : 'warning'" :closable="false"><p>{{ reviewPassed(newestReview) ? '服务端记录复核完整通过，仍需人员确认关闭。' : '复核尚未完整通过或结果不确定，请查看实际复核证据。' }}</p></el-alert><el-empty v-else description="尚未执行业务复核" /><div v-if="closed"><p class="muted">此工单已关闭，处理记录与诊断证据保留。</p><el-descriptions :column="1" border class="space-top"><el-descriptions-item label="人工关闭人">{{ ticket.closed_by || '未提供' }}</el-descriptions-item><el-descriptions-item label="关闭时间">{{ formatTime(ticket.closed_at) }}</el-descriptions-item><el-descriptions-item label="人工例外确认">{{ ticket.manual_override ? '是；请核对下方人工说明' : '否' }}</el-descriptions-item></el-descriptions><h3>人工关闭依据</h3><p>{{ ticket.close_confirmation || '未提供' }}</p></div><p v-if="!reviewer" class="muted">当前账号可调查和记录处理；关闭需由审核员或管理员确认。</p></div></div>
      </template>
    </StatePanel>
    <el-dialog v-model="closeDialog" title="人工确认关闭工单" width="580px" :close-on-click-modal="false"><p>使用最近的复核任务 {{ newestReview?.id }}。此操作只关闭工作台工单。</p><ErrorNotice :error="actionError" /><el-checkbox v-model="closeForm.manual_override">复核未完整通过，使用明确的人工复核说明</el-checkbox><el-alert v-if="closeForm.manual_override" title="请说明未通过项、补充验证和关闭依据，至少 20 个字。" type="warning" :closable="false" class="space-top" /><el-input v-model="closeForm.confirmation" type="textarea" :rows="4" class="space-top" maxlength="3000" show-word-limit placeholder="记录人工确认结论与关闭理由" /><template #footer><el-button @click="closeDialog = false">取消</el-button><el-button type="primary" :loading="acting === 'close'" @click="confirmClose">确认关闭工单</el-button></template></el-dialog>
  </section>
</template>
