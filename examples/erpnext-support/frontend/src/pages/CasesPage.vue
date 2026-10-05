<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, itemsOf } from '../api.js'
import { asLines, canReview } from '../labels.js'
import StatePanel from '../components/StatePanel.vue'
import StatusTag from '../components/StatusTag.vue'

const props = defineProps({ user: Object })
const loading = ref(false), error = ref(null), items = ref([]), saving = ref(false), saveError = ref(null)
const query = ref(''), status = ref(''), version = ref(''), editor = ref(false), reviewing = ref(null)
const review = reactive({ decision: 'approved', note: '' })
const blank = () => ({ id: '', title: '', erpnext_version: '', frappe_version: '', symptom: '', evidence_requirements: '', conditions: '', steps: '', verification: '', source_url: '' })
const form = reactive(blank())
const versions = computed(() => [...new Set(items.value.map(item => item.erpnext_version).filter(Boolean))])
const filtered = computed(() => items.value.filter(item => (!status.value || item.status === status.value) && (!version.value || item.erpnext_version === version.value) && (!query.value.trim() || [item.title, item.symptom, ...(item.conditions || [])].join(' ').toLowerCase().includes(query.value.trim().toLowerCase()))))
function safeLink(value) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null } catch { return null } }
async function load() { loading.value = true; error.value = null; try { items.value = itemsOf(await api.cases()) } catch (failure) { error.value = failure } finally { loading.value = false } }
function edit(item) { const empty = blank(); for (const key of Object.keys(empty)) form[key] = item?.[key] ?? empty[key]; for (const field of ['evidence_requirements', 'conditions', 'steps', 'verification']) form[field] = Array.isArray(item?.[field]) ? item[field].join('\n') : ''; saveError.value = null; editor.value = true }
async function save() {
  if (form.title.trim().length < 3 || form.symptom.trim().length < 5 || !['evidence_requirements', 'conditions', 'steps', 'verification'].every(field => asLines(form[field]).length)) { saveError.value = { message: '标题至少 3 个字，现象至少 5 个字；条件、证据、步骤和复核方法都需要填写。' }; return }
  if (!/^15\.\d+\.\d+$/.test(form.erpnext_version.trim()) || !/^15\.\d+\.\d+$/.test(form.frappe_version.trim())) { saveError.value = { message: '请填写实际验证的完整 v15 版本号，例如 ERPNext 15.85.1、Frappe 15.87.0。' }; return }
  if (!form.source_url.trim().startsWith('https://') || !safeLink(form.source_url.trim())) { saveError.value = { message: '案例需要可追溯的完整 HTTPS 来源链接。' }; return }
  saving.value = true; saveError.value = null
  const data = Object.fromEntries(Object.entries(form).filter(([key]) => key !== 'id').map(([key, value]) => [key, ['evidence_requirements', 'conditions', 'steps', 'verification'].includes(key) ? asLines(value) : value.trim()]))
  try { if (form.id) await api.update('cases', form.id, data); else await api.create('cases', data); editor.value = false; await load(); ElMessage.success('案例已保存为待审核状态。') } catch (failure) { saveError.value = failure } finally { saving.value = false }
}
async function remove(item) { try { await ElMessageBox.confirm(`删除案例“${item.title}”？已保存的诊断记录仍由服务端保留。`, '确认删除案例', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }); await api.remove('cases', item.id); await load(); ElMessage.success('案例已删除。') } catch (failure) { if (failure !== 'cancel' && failure !== 'close') error.value = failure } }
function openReview(item) { reviewing.value = item; review.decision = 'approved'; review.note = ''; saveError.value = null }
async function approve() {
  if (review.note.trim().length < 3) { saveError.value = { message: '请至少用 3 个字说明审核依据或退回原因。' }; return }
  saving.value = true; saveError.value = null
  try { await api.action('cases', reviewing.value.id, 'approve', { decision: review.decision, note: review.note.trim() }); reviewing.value = null; await load(); ElMessage.success('审核结果已记录。') } catch (failure) { saveError.value = failure } finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <section>
    <div class="page-heading"><div><h1>可审核案例库</h1><p>保存有适用条件、证据要求和复核方法的处理经验。编辑后需要重新审核。</p></div><el-button type="primary" @click="edit()">创建案例</el-button></div>
    <div class="panel filter-bar"><el-input v-model="query" placeholder="检索标题、现象、适用条件" clearable class="search-input" /><el-select v-model="status" placeholder="全部审核状态" clearable><el-option label="待审核" value="draft" /><el-option label="已审核" value="approved" /><el-option label="已退回" value="rejected" /></el-select><el-select v-model="version" placeholder="全部 ERPNext 版本" clearable><el-option v-for="item in versions" :key="item" :label="item" :value="item" /></el-select><el-button :loading="loading" @click="load">刷新</el-button></div>
    <StatePanel :loading="loading" :error="error" :empty="!filtered.length" empty-text="没有符合条件的案例，创建案例或调整筛选" @retry="load">
      <div class="case-grid"><article v-for="item in filtered" :key="item.id" class="panel case-card"><div class="case-heading"><h2>{{ item.title }}</h2><StatusTag :value="item.status" /></div><div class="case-versions"><el-tag type="info">ERPNext {{ item.erpnext_version || '未提供' }}</el-tag><el-tag type="info">Frappe {{ item.frappe_version || '未提供' }}</el-tag></div><p>{{ item.symptom }}</p><el-collapse><el-collapse-item title="查看适用条件、证据与处理步骤" :name="item.id"><h3>适用条件</h3><ul class="plain-list"><li v-for="(line, index) in item.conditions || []" :key="index">{{ line }}</li></ul><p v-if="!item.conditions?.length" class="muted">未提供适用条件，不能直接套用。</p><h3>所需证据</h3><ul class="plain-list"><li v-for="(line, index) in item.evidence_requirements || []" :key="index">{{ line }}</li></ul><h3>人工处理步骤</h3><ol class="plain-list"><li v-for="(line, index) in item.steps || []" :key="index">{{ line }}</li></ol><h3>复核方法</h3><ul class="plain-list"><li v-for="(line, index) in item.verification || []" :key="index">{{ line }}</li></ul><a v-if="safeLink(item.source_url)" :href="safeLink(item.source_url)" target="_blank" rel="noopener noreferrer">查看案例来源 ↗</a><p v-if="item.approval_note || item.review_note" class="muted">审核说明：{{ item.approval_note || item.review_note }}</p></el-collapse-item></el-collapse><div class="action-row"><el-button :disabled="user.role === 'analyst' && (item.created_by !== user.username || item.status !== 'draft')" @click="edit(item)">编辑</el-button><el-button v-if="canReview(user)" @click="openReview(item)">审核案例</el-button><el-button type="danger" plain :disabled="!canReview(user)" @click="remove(item)">删除</el-button></div><p v-if="item.status !== 'approved'" class="muted">当前未获审核通过，不作为已验证处理方案。</p></article></div>
    </StatePanel>
    <el-dialog v-model="editor" :title="form.id ? '编辑案例 · 保存后撤销原审核' : '创建案例'" width="760px" :close-on-click-modal="false"><el-alert v-if="saveError" :title="saveError.message" type="error" :closable="false" class="space-bottom" /><el-form label-position="top"><el-form-item label="案例标题"><el-input v-model="form.title" maxlength="160" /></el-form-item><div class="form-columns"><el-form-item label="适用 ERPNext 版本"><el-input v-model="form.erpnext_version" placeholder="例如 15.85.1；按实际验证填写" /></el-form-item><el-form-item label="适用 Frappe 版本"><el-input v-model="form.frappe_version" /></el-form-item></div><el-form-item label="问题现象"><el-input v-model="form.symptom" type="textarea" :rows="2" maxlength="3000" /></el-form-item><div class="form-columns"><el-form-item label="适用条件 · 每行一项"><el-input v-model="form.conditions" type="textarea" :rows="3" /></el-form-item><el-form-item label="证据要求 · 每行一项"><el-input v-model="form.evidence_requirements" type="textarea" :rows="3" /></el-form-item></div><el-form-item label="人工处理步骤 · 每行一步"><el-input v-model="form.steps" type="textarea" :rows="4" /></el-form-item><el-form-item label="复核方法 · 每行一项"><el-input v-model="form.verification" type="textarea" :rows="3" /></el-form-item><el-form-item label="原始 HTTPS 来源链接 · 必填"><el-input v-model="form.source_url" placeholder="https://..." /></el-form-item></el-form><template #footer><el-button @click="editor = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存并待审核</el-button></template></el-dialog>
    <el-dialog :model-value="!!reviewing" title="审核案例" width="580px" :close-on-click-modal="false" @close="reviewing = null"><p>{{ reviewing?.title }}</p><el-alert v-if="saveError" :title="saveError.message" type="error" :closable="false" class="space-bottom" /><el-radio-group v-model="review.decision"><el-radio value="approved">审核通过</el-radio><el-radio value="rejected">退回修改</el-radio></el-radio-group><el-input v-model="review.note" class="space-top" type="textarea" :rows="4" placeholder="填写已核对的版本、适用条件、证据与复核依据，或具体退回原因" /><template #footer><el-button @click="reviewing = null">取消</el-button><el-button type="primary" :loading="saving" @click="approve">提交审核结果</el-button></template></el-dialog>
  </section>
</template>
