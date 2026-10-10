<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { api, setCsrfToken, toError } from './api'
import type { ErrorState, PageName, Session, User } from './types'
import { parseRoute, statusLabel } from './labels'
import StatePanel from './components/StatePanel.vue'
import TicketsPage from './pages/TicketsPage.vue'
import DetailPage from './pages/DetailPage.vue'
import CasesPage from './pages/CasesPage.vue'
import EnvironmentsPage from './pages/EnvironmentsPage.vue'
import UsersPage from './pages/UsersPage.vue'
import AuditPage from './pages/AuditPage.vue'
import ErrorNotice from './components/ErrorNotice.vue'

const user = ref<User | null>(null)
const loading = ref(true)
const sessionError = ref<ErrorState | null>(null)
const submitting = ref(false)
const loginError = ref<ErrorState | null>(null)
const login = reactive({ username: '', password: '' })
const route = ref(parseRoute(window.location.hash))
const navigation: { id: PageName; title: string; description: string; admin?: boolean }[] = [
  { id: 'tickets', title: '工单工作台', description: '提交与跟踪问题' },
  { id: 'detail', title: '诊断详情', description: '证据与人工复核' },
  { id: 'cases', title: '案例库', description: '可审核的处理经验' },
  { id: 'environments', title: '环境配置', description: '版本、能力与授权' },
  { id: 'users', title: '企业账号', description: '角色、授权与停用', admin: true },
  { id: 'audit', title: '审计查询', description: '操作记录与系统状态', admin: true },
]
const visibleNavigation = computed(() => navigation.filter(item => !item.admin || user.value?.role === 'admin'))
const adminRoute = computed(() => ['users', 'audit'].includes(route.value.page) && user.value?.role !== 'admin')
const pageTitle = computed(() => navigation.find((item) => item.id === route.value.page)?.title || '工单工作台')
const onRoute = () => { route.value = parseRoute(window.location.hash) }
const navigate = (page: PageName, id = '') => { window.location.hash = id ? `/tickets/${encodeURIComponent(id)}` : `/${page}` }
function acceptSession(value: Session) { user.value = value.user; setCsrfToken(value.csrf_token) }
async function readSession() {
  loading.value = true; sessionError.value = null
  try { acceptSession(await api.session()) } catch (error) {
    user.value = null
    const failure = toError(error)
    if (failure.status !== 401) sessionError.value = failure
  } finally { loading.value = false }
}
async function signIn() {
  if (!login.username.trim() || !login.password) { loginError.value = { message: '请输入账号和密码。' }; return }
  submitting.value = true; loginError.value = null
  try { acceptSession(await api.login({ username: login.username.trim(), password: login.password })); login.password = '' }
  catch (error) { loginError.value = toError(error); login.password = '' }
  finally { submitting.value = false }
}
async function signOut() {
  submitting.value = true
  try { await api.logout(); user.value = null; setCsrfToken(''); navigate('tickets') }
  catch (error) { sessionError.value = toError(error) }
  finally { submitting.value = false }
}
function sessionExpired(event: Event) {
  user.value = null; setCsrfToken('')
  loginError.value = event instanceof CustomEvent ? toError(event.detail) : { message: '登录已失效，请重新登录。' }
}
onMounted(() => { window.addEventListener('hashchange', onRoute); window.addEventListener('session-expired', sessionExpired); readSession() })
onBeforeUnmount(() => { window.removeEventListener('hashchange', onRoute); window.removeEventListener('session-expired', sessionExpired) })
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div v-if="!user" class="login-shell">
      <div class="login-card">
        <div class="brand-mark">H</div><p class="eyebrow">HOLMES · ERPNext 售后支持</p>
        <h1>库存与发货诊断工作台</h1>
        <p class="muted">从实际单据和库存证据开始调查，处理与关闭由人员确认。</p>
        <StatePanel :loading="loading" :error="sessionError" @retry="readSession">
          <ErrorNotice :error="loginError" />
          <el-form label-position="top" @submit.prevent="signIn">
            <el-form-item label="账号"><el-input v-model="login.username" autocomplete="username" placeholder="部署时配置的工作台账号" /></el-form-item>
            <el-form-item label="密码"><el-input v-model="login.password" type="password" show-password autocomplete="current-password" @keyup.enter="signIn" /></el-form-item>
            <el-button type="primary" class="full-width" :loading="submitting" @click="signIn">登录工作台</el-button>
          </el-form>
          <p class="login-note">仅连接已授权环境。凭据由服务端管理，工作台不会自动修改 ERP 业务数据。</p>
        </StatePanel>
      </div>
    </div>
    <div v-else class="workbench-shell">
      <aside class="sidebar">
        <div class="sidebar-brand"><span class="brand-mark">H</span><div><strong>发货诊断</strong><small>ERPNext 售后工作台</small></div></div>
        <nav aria-label="工作台导航">
          <button v-for="item in visibleNavigation" :key="item.id" :class="{ active: route.page === item.id }" @click="navigate(item.id)">
            <span>{{ item.title }}</span><small>{{ item.description }}</small>
          </button>
        </nav>
        <div class="sidebar-note">ERP 只读调查<br />人工处理 · 证据复核</div>
      </aside>
      <div class="workspace">
        <header class="topbar"><div><span class="eyebrow">售后支持</span><strong>{{ pageTitle }}</strong></div><div class="session-meta"><span>{{ user.username }}</span><el-tag type="info">{{ statusLabel(user.role) }}</el-tag><el-button link :loading="submitting" @click="signOut">退出</el-button></div></header>
        <main>
          <ErrorNotice :error="sessionError" />
          <StatePanel v-if="adminRoute" :error="{status: 403, message: '企业账号与审计查询仅限管理员。'}" @retry="navigate('tickets')" />
          <TicketsPage v-else-if="route.page === 'tickets'" :user="user" @open="(id) => navigate('detail', id)" />
          <DetailPage v-else-if="route.page === 'detail'" :user="user" :ticket-id="route.id" @back="navigate('tickets')" />
          <CasesPage v-else-if="route.page === 'cases'" :user="user" />
          <EnvironmentsPage v-else-if="route.page === 'environments'" :user="user" />
          <UsersPage v-else-if="route.page === 'users'" :user="user" />
          <AuditPage v-else-if="route.page === 'audit'" :user="user" />
        </main>
      </div>
    </div>
  </el-config-provider>
</template>
