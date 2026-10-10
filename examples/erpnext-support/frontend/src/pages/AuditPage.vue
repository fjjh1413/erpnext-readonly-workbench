<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api, toError } from '../api'
import { usePagedList } from '../usePagedList'
import { asText, formatTime } from '../labels'
import type { AuditEvent, ErrorState, SystemStatus, User } from '../types'
import StatePanel from '../components/StatePanel.vue'
import PageNavigation from '../components/PageNavigation.vue'
import ErrorNotice from '../components/ErrorNotice.vue'
const props = defineProps<{ user: User }>()
const admin = computed(() => props.user.role === 'admin')
const filters = reactive({ q: '', actor: '', action: '', outcome: '', resource_kind: '' })
const { items, loading, error, pagination, load, reset } = usePagedList<AuditEvent>(api.audit, () => ({ ...filters }))
const status = ref<SystemStatus | null>(null), statusLoading = ref(false), statusError = ref<ErrorState | null>(null)
const denied: ErrorState = { status: 403, message: '审计查询与系统状态仅限管理员。' }
async function readStatus() { statusLoading.value = true; statusError.value = null; try { status.value = await api.systemStatus() } catch (failure) { status.value = null; statusError.value = toError(failure) } finally { statusLoading.value = false } }
function refresh() { void load(); void readStatus() }
watch(filters, () => { if (admin.value) reset() })
onMounted(() => { if (admin.value) refresh() })
</script>
<template>
  <section>
    <div class="page-heading"><div><h1>企业审计查询</h1><p>查看服务端记录的实际操作、结果与请求编号。审计详情已由服务端脱敏。</p></div><el-button :disabled="!admin" :loading="loading || statusLoading" @click="refresh">刷新审计与状态</el-button></div>
    <StatePanel v-if="!admin" :error="denied" />
    <template v-else>
      <div class="panel space-bottom"><h2>系统状态</h2><ErrorNotice :error="statusError" /><p v-if="statusLoading" class="muted">正在读取实际系统状态…</p><el-descriptions v-else-if="status" :column="3" border><el-descriptions-item label="工作台版本">{{ status.version }}</el-descriptions-item><el-descriptions-item label="工单存储">{{ status.storage_backend }}</el-descriptions-item><el-descriptions-item label="任务队列">{{ status.queue_backend }}</el-descriptions-item><el-descriptions-item label="执行进程"><el-tag :type="status.worker_ready ? 'success' : 'warning'">{{ status.worker_ready ? '已就绪' : '尚未就绪' }}</el-tag></el-descriptions-item><el-descriptions-item label="工作台数据库"><el-tag :type="status.database_ready ? 'success' : 'warning'">{{ status.database_ready === true ? '已就绪' : status.database_ready === false ? '尚未就绪' : '未报告' }}</el-tag></el-descriptions-item></el-descriptions><p class="muted">工作台状态不代表 ERP 业务完成，也不证明模型可用。</p></div>
      <div class="panel filter-bar"><el-input v-model="filters.q" clearable placeholder="检索请求编号、资源或操作详情" class="search-input" /><el-input v-model="filters.actor" clearable placeholder="操作账号" /><el-input v-model="filters.action" clearable placeholder="操作名称" /><el-select v-model="filters.outcome" clearable placeholder="全部执行结果"><el-option label="成功" value="success" /><el-option label="权限拒绝" value="denied" /><el-option label="失败" value="error" /></el-select><el-input v-model="filters.resource_kind" clearable placeholder="资源类型" /></div>
      <StatePanel :loading="loading" :error="error" :empty="!items.length" empty-text="没有符合条件的审计记录" @retry="load">
        <div class="panel"><el-table :data="items" row-key="id" stripe><el-table-column type="expand"><template #default="{row}"><div class="audit-detail"><p>审计编号：{{ row.id }} · 请求编号：{{ row.request_id || '未记录' }}</p><pre class="evidence-content">{{ asText(row.detail) }}</pre></div></template></el-table-column><el-table-column label="操作时间" min-width="170"><template #default="{row}">{{ formatTime(row.created_at) }}</template></el-table-column><el-table-column prop="actor" label="操作账号" min-width="125" /><el-table-column prop="action" label="操作" min-width="165" /><el-table-column label="资源" min-width="180"><template #default="{row}">{{ row.resource_kind }}<p class="muted">{{ row.resource_id || '未关联资源' }}</p></template></el-table-column><el-table-column label="结果" width="110"><template #default="{row}"><el-tag :type="row.outcome === 'success' ? 'success' : row.outcome === 'denied' ? 'warning' : 'danger'">{{ row.outcome === 'success' ? '成功' : row.outcome === 'denied' ? '权限拒绝' : row.outcome === 'error' ? '失败' : row.outcome }}</el-tag></template></el-table-column><el-table-column prop="request_id" label="请求编号" min-width="200" /></el-table></div>
      </StatePanel>
      <PageNavigation v-model:page="pagination.page" v-model:limit="pagination.limit" :total="pagination.total" :loading="loading" @change="load" />
    </template>
  </section>
</template>
