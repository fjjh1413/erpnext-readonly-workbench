<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, itemsOf, toError } from '../api'
import { usePagedList } from '../usePagedList'
import { statusLabel } from '../labels'
import type { Environment, ErrorState, Role, User } from '../types'
import StatePanel from '../components/StatePanel.vue'
import ErrorNotice from '../components/ErrorNotice.vue'
import PageNavigation from '../components/PageNavigation.vue'

const props = defineProps<{ user: User }>()
const admin = computed(() => props.user.role === 'admin')
const filters = reactive({ q: '', role: '', disabled: '' })
const { items, loading, error, pagination, load, reset } = usePagedList<User>(api.users, () => ({ ...filters }))
const environments = ref<Environment[]>([]), environmentLoading = ref(false)
const editor = ref(false), saving = ref(false), saveError = ref<ErrorState | null>(null)
const form = reactive({ id: '', username: '', password: '', role: 'analyst' as Role, environment_ids: [] as string[], disabled: false })
const passwordUser = ref<User | null>(null), newPassword = ref(''), passwordSaving = ref(false), passwordError = ref<ErrorState | null>(null)
const denied: ErrorState = { status: 403, message: '企业账号管理仅限管理员。' }
const environmentName = (id: string) => environments.value.find(item => item.id === id)?.name || id
async function loadEnvironments(q = '') { environmentLoading.value = true; try { const next = itemsOf<Environment>(await api.environments({ q, limit: 100 })); environments.value = [...environments.value.filter(item => form.environment_ids.includes(item.id) && !next.some(value => value.id === item.id)), ...next] } catch (failure) { saveError.value = toError(failure) } finally { environmentLoading.value = false } }
function edit(item?: User) { Object.assign(form, { id: item?.id || '', username: item?.username || '', password: '', role: item?.role || 'analyst', environment_ids: [...(item?.environment_ids || [])], disabled: item?.disabled || false }); saveError.value = null; editor.value = true }
async function save() {
  if (!form.id && (!/^[A-Za-z0-9_.-]{3,64}$/.test(form.username) || form.password.length < 12)) { saveError.value = { message: '账号需为 3–64 位字母、数字、点、下划线或连字符；初始密码至少 12 位。' }; return }
  saving.value = true; saveError.value = null
  try {
    if (form.id) await api.updateUser(form.id, { role: form.role, environment_ids: [...form.environment_ids], disabled: form.disabled })
    else await api.create<User>('users', { username: form.username, password: form.password, role: form.role, environment_ids: [...form.environment_ids] })
    editor.value = false; await load(); ElMessage.success('企业账号与授权已保存。')
  } catch (failure) { saveError.value = toError(failure) }
  finally { form.password = ''; saving.value = false }
}
function openPassword(item: User) { passwordUser.value = item; newPassword.value = ''; passwordError.value = null }
async function resetPassword() {
  const selected = passwordUser.value
  if (!selected) return
  if (newPassword.value.length < 12) { passwordError.value = { message: '新密码至少需要 12 位。' }; return }
  passwordSaving.value = true; passwordError.value = null
  try { await api.resetPassword(selected.id, newPassword.value); passwordUser.value = null; ElMessage.success('密码已重置；请通过企业受控渠道通知本人。') }
  catch (failure) { passwordError.value = toError(failure) }
  finally { newPassword.value = ''; passwordSaving.value = false }
}
watch(filters, () => { if (admin.value) reset() })
onMounted(() => { if (admin.value) { void load(); void loadEnvironments() } })
</script>
<template>
  <section>
    <div class="page-heading"><div><h1>企业账号与授权</h1><p>单企业内部协作：分配角色和可访问环境，停用离职或不再需要的账号。</p></div><el-button type="primary" :disabled="!admin" @click="edit()">创建企业账号</el-button></div>
    <StatePanel v-if="!admin" :error="denied" />
    <template v-else>
      <div class="panel filter-bar"><el-input v-model="filters.q" clearable placeholder="检索企业账号" class="search-input" /><el-select v-model="filters.role" clearable placeholder="全部角色"><el-option v-for="role in ['admin', 'analyst', 'reviewer']" :key="role" :value="role" :label="statusLabel(role)" /></el-select><el-select v-model="filters.disabled" clearable placeholder="全部账号状态"><el-option label="已启用" value="false" /><el-option label="已停用" value="true" /></el-select><el-button :loading="loading" @click="load">刷新账号</el-button></div>
      <StatePanel :loading="loading" :error="error" :empty="!items.length" empty-text="没有符合条件的企业账号" @retry="load">
        <div class="panel"><el-table :data="items" row-key="id" stripe><el-table-column prop="username" label="账号" min-width="160" /><el-table-column label="角色" width="150"><template #default="{row}">{{ statusLabel(row.role) }}</template></el-table-column><el-table-column label="状态" width="110"><template #default="{row}"><el-tag :type="row.disabled ? 'danger' : 'success'">{{ row.disabled ? '已停用' : '已启用' }}</el-tag></template></el-table-column><el-table-column label="环境授权" min-width="250"><template #default="{row}"><span v-if="row.role === 'admin'">管理员可管理企业环境</span><span v-else-if="!row.environment_ids?.length" class="muted">未授权环境</span><el-tag v-for="id in row.role === 'admin' ? [] : row.environment_ids" :key="id" class="grant-tag">{{ environmentName(id) }}</el-tag></template></el-table-column><el-table-column label="操作" width="205"><template #default="{row}"><el-button link type="primary" @click="edit(row)">角色与授权</el-button><el-button link @click="openPassword(row)">重置密码</el-button></template></el-table-column></el-table></div>
      </StatePanel>
      <PageNavigation v-model:page="pagination.page" v-model:limit="pagination.limit" :total="pagination.total" :loading="loading" @change="load" />
      <p class="muted">账号权限仍由服务端逐次校验。账号变更与密码重置记录可在审计查询中核对。</p>
    </template>
    <el-dialog v-model="editor" :title="form.id ? '编辑企业账号权限' : '创建企业账号'" width="620px" :close-on-click-modal="false" @closed="form.password = ''">
      <ErrorNotice :error="saveError" />
      <el-form label-position="top"><el-form-item label="企业账号"><el-input v-model="form.username" :disabled="!!form.id" autocomplete="off" maxlength="64" /></el-form-item><el-form-item v-if="!form.id" label="初始密码 · 至少 12 位"><el-input v-model="form.password" type="password" autocomplete="new-password" maxlength="256" /></el-form-item><el-form-item label="企业角色"><el-select v-model="form.role" :disabled="form.id === user.id" class="full-width"><el-option v-for="role in ['admin', 'analyst', 'reviewer']" :key="role" :value="role" :label="statusLabel(role)" /></el-select></el-form-item><el-form-item label="可访问环境"><el-select v-model="form.environment_ids" multiple filterable remote :remote-method="loadEnvironments" :loading="environmentLoading" :disabled="form.role === 'admin'" class="full-width" placeholder="输入环境名称检索并选择"><el-option v-for="item in environments" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item><p v-if="form.role === 'admin'" class="muted">管理员按服务端企业管理权限访问环境，无需逐个分配。</p><el-checkbox v-if="form.id" v-model="form.disabled" :disabled="form.id === user.id">停用此账号</el-checkbox><p v-if="form.id === user.id" class="muted">当前页面不允许停用自身或更改自身角色。请由另一位管理员处理。</p></el-form>
      <template #footer><el-button @click="editor = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存账号与授权</el-button></template>
    </el-dialog>
    <el-dialog :model-value="!!passwordUser" title="重置企业账号密码" width="560px" :close-on-click-modal="false" @close="passwordUser = null; newPassword = ''"><p>为 {{ passwordUser?.username }} 设置新密码。此页不会返回原密码，也不会在页面持久保存密码。</p><ErrorNotice :error="passwordError" /><el-input v-model="newPassword" type="password" autocomplete="new-password" maxlength="256" placeholder="输入至少 12 位新密码" /><template #footer><el-button @click="passwordUser = null; newPassword = ''">取消</el-button><el-button type="primary" :loading="passwordSaving" @click="resetPassword">确认重置密码</el-button></template></el-dialog>
  </section>
</template>
