<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, itemsOf } from '../api.js'
import { formatTime, statusLabel } from '../labels.js'
import StatePanel from '../components/StatePanel.vue'
import StatusTag from '../components/StatusTag.vue'

const props = defineProps({ user: Object })
const emit = defineEmits(['open'])
const loading = ref(true), error = ref(null), tickets = ref([]), environments = ref([]), users = ref([])
const filters = reactive({ q: '', status: '', environment_id: '', owner: '' })
const showCreate = ref(false), saving = ref(false), saveError = ref(null), ownerSaving = ref('')
const form = reactive({ environment_id: '', delivery_note: '', description: '', occurred_at: null, operator_user: '', owner: '' })
const stateOptions = ['pending', 'investigating', 'awaiting_action', 'needs_information', 'awaiting_review', 'closed']
const environmentName = (id) => environments.value.find((item) => String(item.id) === String(id))?.name || `环境 ${id}`
const rows = computed(() => tickets.value.filter((item) => {
  const q = filters.q.trim().toLowerCase()
  return (!q || `${item.delivery_note} ${item.description} ${item.id}`.toLowerCase().includes(q)) &&
    (!filters.status || item.status === filters.status) &&
    (!filters.environment_id || String(item.environment_id) === String(filters.environment_id)) &&
    (!filters.owner || item.owner === filters.owner)
}))
const openCount = computed(() => tickets.value.filter((item) => item.status !== 'closed').length)
async function load() {
  loading.value = true; error.value = null
  try {
    const values = await Promise.all([api.tickets(), api.environments(), api.users()])
    tickets.value = itemsOf(values[0]); environments.value = itemsOf(values[1]); users.value = itemsOf(values[2])
  } catch (failure) { tickets.value = []; error.value = failure }
  finally { loading.value = false }
}
function openCreate() {
  Object.assign(form, { environment_id: '', delivery_note: '', description: '', occurred_at: null, operator_user: '', owner: props.user.username })
  saveError.value = null; showCreate.value = true
}
async function createTicket() {
  if (!form.environment_id || !form.delivery_note.trim() || !form.description.trim() || !form.occurred_at) {
    saveError.value = { message: '请填写环境、发货单号、问题描述和发生时间。' }; return
  }
  saving.value = true; saveError.value = null
  try {
    const value = await api.create('tickets', {
      ...form, delivery_note: form.delivery_note.trim(), description: form.description.trim(),
      occurred_at: new Date(form.occurred_at).toISOString(), operator_user: form.operator_user.trim() || null,
    })
    showCreate.value = false; await load(); ElMessage.success('工单已创建。')
    if (value?.id) emit('open', value.id)
  } catch (failure) { saveError.value = failure }
  finally { saving.value = false }
}
async function changeOwner(ticket, value) {
  ownerSaving.value = ticket.id
  try { await api.update('tickets', ticket.id, { owner: value }, 'PATCH'); ElMessage.success('负责人已更新。'); await load() }
  catch (failure) { error.value = failure; tickets.value = [] }
  finally { ownerSaving.value = '' }
}
onMounted(load)
</script>

<template>
  <section>
    <div class="page-heading"><div><h1>工单工作台</h1><p>选择客户环境，记录发货问题，跟进调查与复核。</p></div><el-button type="primary" :disabled="loading || !!error" @click="openCreate">新建工单</el-button></div>
    <div class="summary-strip"><span>已读取 <strong>{{ tickets.length }}</strong> 个工单</span><span>未关闭 <strong>{{ openCount }}</strong> 个</span><span class="muted">仅展示当前账号获准访问的环境</span></div>
    <div class="panel">
      <div class="filter-bar">
        <el-input v-model="filters.q" clearable placeholder="搜索发货单号或问题描述" aria-label="搜索工单" />
        <el-select v-model="filters.environment_id" clearable placeholder="全部环境" aria-label="环境筛选"><el-option v-for="item in environments" :key="item.id" :label="item.name" :value="item.id" /></el-select>
        <el-select v-model="filters.status" clearable placeholder="全部状态" aria-label="状态筛选"><el-option v-for="item in stateOptions" :key="item" :label="statusLabel(item)" :value="item" /></el-select>
        <el-select v-model="filters.owner" clearable placeholder="全部负责人" aria-label="负责人筛选"><el-option v-for="item in users" :key="item.id" :label="item.username" :value="item.username" /></el-select>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
      <StatePanel :loading="loading" :error="error" :empty="!rows.length" :empty-text="tickets.length ? '没有符合筛选条件的工单' : '还没有工单，请先新建一张发货问题工单'" @retry="load">
        <el-table :data="rows" row-key="id" stripe>
          <el-table-column label="发货问题" min-width="235"><template #default="{ row }"><el-button link type="primary" @click="emit('open', row.id)">{{ row.delivery_note }}</el-button><p class="table-description">{{ row.description }}</p><small class="muted">工单 {{ row.id }}</small></template></el-table-column>
          <el-table-column label="客户环境" min-width="135"><template #default="{ row }">{{ environmentName(row.environment_id) }}</template></el-table-column>
          <el-table-column label="状态" width="135"><template #default="{ row }"><StatusTag :value="row.status" /></template></el-table-column>
          <el-table-column label="负责人" min-width="150"><template #default="{ row }"><el-select :model-value="row.owner || ''" size="small" placeholder="未分配" :disabled="ownerSaving === row.id || row.status === 'closed'" @change="(value) => changeOwner(row, value)"><el-option v-for="item in users" :key="item.id" :label="item.username" :value="item.username" /></el-select></template></el-table-column>
          <el-table-column label="发生时间" min-width="165"><template #default="{ row }">{{ formatTime(row.occurred_at) }}</template></el-table-column>
          <el-table-column prop="submitter" label="提交人" width="110" />
          <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click="emit('open', row.id)">查看诊断</el-button></template></el-table-column>
        </el-table>
      </StatePanel>
    </div>
    <el-dialog v-model="showCreate" title="新建发货问题工单" width="620px" :close-on-click-modal="false">
      <el-alert v-if="saveError" :title="saveError.status === 403 ? '权限不足：' + saveError.message : saveError.message" type="error" :closable="false" class="space-bottom" />
      <el-form :model="form" label-position="top" @submit.prevent="createTicket">
        <el-form-item label="客户环境" required><el-select v-model="form.environment_id" class="full-width" placeholder="选择已授权环境"><el-option v-for="item in environments" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item>
        <el-form-item label="Delivery Note 发货单号" required><el-input v-model="form.delivery_note" maxlength="140" placeholder="输入 ERPNext 中的真实发货单号" /></el-form-item>
        <el-form-item label="问题描述" required><el-input v-model="form.description" type="textarea" :rows="4" maxlength="3000" show-word-limit placeholder="记录实际错误、操作步骤及对业务的影响" /></el-form-item>
        <div class="form-grid"><el-form-item label="发生时间（本地时间）" required><el-date-picker v-model="form.occurred_at" type="datetime" placeholder="选择故障发生时间" class="full-width" /></el-form-item><el-form-item label="负责人"><el-select v-model="form.owner" class="full-width"><el-option v-for="item in users" :key="item.id" :label="item.username" :value="item.username" /></el-select></el-form-item></div>
        <el-form-item label="故障操作用户（ERPNext，可选）"><el-input v-model="form.operator_user" placeholder="诊断账号能读取，不代表此用户有提交权限" maxlength="140" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="showCreate = false">取消</el-button><el-button type="primary" :loading="saving" @click="createTicket">创建工单</el-button></template>
    </el-dialog>
  </section>
</template>
