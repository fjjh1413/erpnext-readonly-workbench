// Real UI integration. No route mocks, saved sessions, HAR, traces or password output.
import assert from 'node:assert/strict'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { randomBytes, randomUUID } from 'node:crypto'
import { performance } from 'node:perf_hooks'
import { chromium } from 'playwright'

const args = process.argv.slice(2)
const option = (name, fallback) => { const index = args.indexOf(name); return index < 0 ? fallback : args[index + 1] }
const origin = new URL(option('--url', 'http://127.0.0.1:8765'))
assert.equal(origin.protocol, 'http:', 'Only the explicit local HTTP workbench is used by this test.')
assert.equal(origin.hostname, '127.0.0.1', 'Production/non-loopback connections are forbidden.')
assert.equal(origin.pathname, '/', 'Use a workbench origin without a path.')
const channel = option('--channel', 'msedge')
assert.ok(['msedge', 'chrome'].includes(channel), 'Use an installed Edge or Chrome channel.')
const started = performance.now()
const browser = await chromium.launch({ channel, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } })
const page = await context.newPage()
let browserErrors = 0
page.on('pageerror', () => { browserErrors++ })
const checks = []
let stage = 'browser_ready'
const output = resolve('.qa', new Date().toISOString().replaceAll(':', '-') + '-' + randomUUID().slice(0, 8))
async function visible(locator, timeout = 15000) { await locator.waitFor({ state: 'visible', timeout }) }
async function formField(label, value, dialog = page) { await dialog.locator('.el-form-item').filter({ hasText: label }).locator('input, textarea').first().fill(value) }
async function json(response) { assert.ok(response.ok(), `Local API returned HTTP ${response.status()}`); return await response.json() }
async function sessionHeaders(activePage = page) { const session = await json(await activePage.request.get(`${origin.origin}/api/session`)); return { 'X-CSRF-Token': session.csrf_token } }
async function login(activePage, account) { await activePage.goto(origin.origin); await activePage.locator('input[autocomplete="username"]').fill(account.username); await activePage.locator('input[autocomplete="current-password"]').fill(account.password); await activePage.getByRole('button', { name: '登录工作台', exact: true }).click(); await visible(activePage.getByRole('heading', { name: '工单工作台', exact: true })); assert.equal(await activePage.locator('input[type="password"]').count(), 0) }
async function settleTask(ticketId, kind = 'diagnosis') {
  const deadline = performance.now() + 150000
  while (performance.now() < deadline) {
    const ticket = await json(await page.request.get(`${origin.origin}/api/tickets/${ticketId}`))
    const task = ticket.tasks[0]
    if (task?.kind === kind && !['queued', 'running'].includes(task.state)) { assert.ok(['completed', 'partial'].includes(task.state), `Actual task ended ${task.state}`); return { ticket, task } }
    await new Promise(done => setTimeout(done, 500))
  }
  throw new Error('Actual diagnostic task exceeded the E2E wait limit.')
}

try {
  if (args.includes('--probe')) {
    await page.goto('about:blank')
    assert.equal(page.url(), 'about:blank')
    console.log(JSON.stringify({ browser_available: true, channel, browser_version: browser.version(), isolated_context: true, elapsed_seconds: (performance.now() - started) / 1000 }))
  } else {
    const credentialsPath = resolve(option('--credentials', '../.runtime/workbench/initial-login.json'))
    const account = JSON.parse(await readFile(credentialsPath, 'utf8'))
    const deliveryNote = option('--delivery-note')
    assert.ok(deliveryNote, 'Provide --delivery-note with an actual isolated ERP fixture ID.')
    stage = 'login'; await login(page, account)
    checks.push('actual_login')
    const environments = (await json(await page.request.get(`${origin.origin}/api/environments`))).items
    const environment = environments.find(item => item.id === option('--environment-id')) || (environments.length === 1 ? environments[0] : null)
    assert.ok(environment, 'Provide --environment-id when more than one environment is configured.')
    assert.ok(['http://127.0.0.1:8090', 'http://127.0.0.1:8090/'].includes(environment.base_url), 'Only the isolated local ERP lab is eligible.')
    stage = 'create_ticket'; await page.getByRole('button', { name: '新建工单', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await visible(dialog)
    await dialog.locator('.el-form-item').filter({ hasText: '客户环境' }).locator('.el-select').click()
    await page.getByRole('option', { name: environment.name, exact: true }).click()
    await formField('发货单号', deliveryNote, dialog)
    await formField('问题描述', '独立浏览器集成测试：核对隔离实验室发货证据与人工闭环。', dialog)
    const localDate = new Date()
    const pad = value => String(value).padStart(2, '0')
    const occurredAt = `${localDate.getFullYear()}-${pad(localDate.getMonth() + 1)}-${pad(localDate.getDate())} ${pad(localDate.getHours())}:${pad(localDate.getMinutes())}:${pad(localDate.getSeconds())}`
    await formField('发生时间', occurredAt, dialog)
    await dialog.locator('.el-form-item').filter({ hasText: '发生时间' }).locator('input').press('Tab')
    await dialog.getByRole('button', { name: '创建工单', exact: true }).click()
    await page.waitForURL(/#\/tickets\/[a-f0-9]{32}$/)
    const ticketId = page.url().split('/').at(-1)
    await visible(page.getByRole('heading', { name: '诊断详情', exact: true }))
    stage = 'start_diagnosis'; await page.getByRole('button', { name: '开始诊断', exact: true }).click()
    const diagnostic = await settleTask(ticketId)
    await page.getByRole('button', { name: '刷新详情', exact: true }).click()
    await visible(page.getByRole('heading', { name: '证据与工具调用', exact: true }))
    assert.ok(diagnostic.task.result?.evidence?.length > 0, 'Actual diagnosis must retain collected evidence.')
    stage = 'expand_evidence'; await page.locator('#evidence-panel .el-collapse-item__header').first().click()
    await visible(page.locator('#evidence-panel .evidence-content').first())
    checks.push('create_ticket', 'actual_diagnosis', 'expand_actual_evidence')
    await mkdir(output, { recursive: true })
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: resolve(output, 'diagnosis.png'), fullPage: true })
    stage = 'record_human_action'; await page.locator('textarea[placeholder="记录处理人、实际动作、执行时间及结果"]').fill('浏览器QA：已人工核对本隔离样例的发货单与库存流水。未修改ERP；仅测试工作台记录。')
    await page.getByRole('button', { name: '保存处理记录', exact: true }).click()
    await visible(page.getByText('浏览器QA：已人工核对本隔离样例的发货单与库存流水。未修改ERP；仅测试工作台记录。', { exact: true }))
    stage = 'start_review'; await page.getByRole('button', { name: '读取业务状态并复核', exact: true }).click()
    const reviewed = await settleTask(ticketId, 'review')
    assert.equal(reviewed.task.kind, 'review')
    await page.getByRole('button', { name: '刷新详情', exact: true }).click()
    stage = 'manual_close'; await page.getByRole('button', { name: '人工确认关闭', exact: true }).click()
    const closeDialog = page.getByRole('dialog')
    await visible(closeDialog)
    if (!reviewed.task.result?.review?.business_complete) {
      await closeDialog.locator('label').filter({ hasText: '复核未完整通过，使用明确的人工复核说明' }).click()
      assert.equal(await closeDialog.locator('input[type="checkbox"]').isChecked(), true)
    }
    await closeDialog.locator('textarea').fill('仅关闭独立浏览器QA测试工单：已核对实际复核证据与未通过项。未宣称原故障修复，ERP数据未变更。')
    const closureResponse = page.waitForResponse(response => response.url().endsWith(`/api/tickets/${ticketId}/close`) && response.request().method() === 'POST')
    await closeDialog.getByRole('button', { name: '确认关闭工单', exact: true }).click()
    await json(await closureResponse)
    const closedTicket = await json(await page.request.get(`${origin.origin}/api/tickets/${ticketId}`))
    assert.equal(closedTicket.status, 'closed')
    checks.push('record_actual_human_action', 'actual_business_review', 'manual_close')
    stage = 'case_workspace'; await page.getByRole('button', { name: /案例库/ }).click()
    await visible(page.getByRole('heading', { name: '可审核案例库', exact: true }))
    checks.push('case_workspace')
    stage = 'case_create'
    const caseTitle = '浏览器QA实际证据复核-' + randomUUID().slice(0, 8)
    await page.getByRole('button', { name: '创建案例', exact: true }).click()
    const caseDialog = page.getByRole('dialog')
    await visible(caseDialog)
    await formField('案例标题', caseTitle, caseDialog)
    await formField('适用 ERPNext', environment.erpnext_version, caseDialog)
    await formField('适用 Frappe', environment.frappe_version, caseDialog)
    await formField('问题现象', `本地已提交样例 ${deliveryNote} 已实际采证；此案例仅验证工作台操作，不指认业务根因。`, caseDialog)
    await formField('适用条件', '只适用于本机隔离实验室QA\n不得作为客户修复方案', caseDialog)
    await formField('证据要求', diagnostic.task.result.evidence.map(item => item.id + ' ' + item.source).join('\n'), caseDialog)
    await formField('人工处理步骤', '由人员核对已取得的发货单提交状态和对应出库流水\n仅记录实际核对，不执行ERP写入', caseDialog)
    await formField('复核方法', '重读实际业务状态并与人工核对记录对照', caseDialog)
    await formField('来源链接', 'https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/doctype/delivery_note/delivery_note.py', caseDialog)
    const caseResponse = page.waitForResponse(response => response.url().endsWith('/api/cases') && response.request().method() === 'POST')
    await caseDialog.getByRole('button', { name: '保存并待审核', exact: true }).click()
    const createdCase = await json(await caseResponse)
    assert.equal(createdCase.status, 'draft')
    await visible(page.getByRole('heading', { name: caseTitle, exact: true }))
    await page.getByPlaceholder('检索标题、现象、适用条件').fill(caseTitle)
    const caseCard = page.locator('.case-card').filter({ hasText: caseTitle })
    assert.equal(await caseCard.count(), 1)
    stage = 'case_approval'
    await caseCard.getByRole('button', { name: '审核案例', exact: true }).click()
    const approvalDialog = page.getByRole('dialog')
    await approvalDialog.locator('textarea').fill('QA人工核对：版本与采集证据匹配，仅用于隔离工作台操作验收，不能作为客户修复方案。')
    const approvalResponse = page.waitForResponse(response => response.url().endsWith(`/api/cases/${createdCase.id}/approve`))
    await approvalDialog.getByRole('button', { name: '提交审核结果', exact: true }).click()
    assert.equal((await json(await approvalResponse)).status, 'approved')
    stage = 'case_edit_revoke_approval'
    await caseCard.getByRole('button', { name: '编辑', exact: true }).click()
    const editCaseDialog = page.getByRole('dialog')
    await formField('问题现象', `编辑后的QA案例：${deliveryNote} 的实际采集证据只用于隔离验收，不证明客户问题。`, editCaseDialog)
    const editCaseResponse = page.waitForResponse(response => response.url().endsWith(`/api/cases/${createdCase.id}`) && response.request().method() === 'PUT')
    await editCaseDialog.getByRole('button', { name: '保存并待审核', exact: true }).click()
    assert.equal((await json(await editCaseResponse)).status, 'draft')
    await editCaseDialog.waitFor({ state: 'hidden' })
    await visible(caseCard.getByText('待审核', { exact: true }))
    await page.screenshot({ path: resolve(output, 'case.png'), fullPage: true })
    stage = 'case_delete_own_qa_data'
    await caseCard.getByRole('button', { name: '删除', exact: true }).click()
    const deleteCaseResponse = page.waitForResponse(response => response.url().endsWith(`/api/cases/${createdCase.id}`) && response.request().method() === 'DELETE')
    await page.getByRole('dialog').getByRole('button', { name: '删除', exact: true }).click()
    await json(await deleteCaseResponse)
    assert.ok(!(await json(await page.request.get(`${origin.origin}/api/cases`))).items.some(item => item.id === createdCase.id))
    checks.push('case_create', 'case_search', 'case_human_approve', 'case_edit_revokes_approval', 'case_delete_own_qa_data')
    stage = 'environment_workspace'; await page.getByRole('button', { name: /环境配置/ }).click()
    await visible(page.getByRole('heading', { name: '环境与只读能力', exact: true }))
    await page.getByRole('button', { name: '连接与能力检查', exact: true }).first().click()
    await visible(page.getByRole('heading', { name: '实际检查结果', exact: true }), 45000)
    checks.push('actual_environment_check')
    await page.screenshot({ path: resolve(output, 'environment.png'), fullPage: true })
    stage = 'environment_create'
    const environmentTitle = '浏览器QA同一隔离站点-' + randomUUID().slice(0, 8)
    await page.getByRole('button', { name: '新增环境', exact: true }).click()
    const environmentDialog = page.getByRole('dialog')
    await visible(environmentDialog)
    await formField('环境名称', environmentTitle, environmentDialog)
    await formField('ERPNext 地址', environment.base_url, environmentDialog)
    await formField('服务端凭据别名', environment.credential_ref, environmentDialog)
    await formField('ERPNext 完整版本', environment.erpnext_version, environmentDialog)
    await formField('Frappe 完整版本', environment.frappe_version, environmentDialog)
    await formField('已安装应用', environment.installed_apps.join('\n'), environmentDialog)
    await formField('只读运行采集器别名', 'erp-lab', environmentDialog)
    for (const [service, label] of [['application', '应用容器与限定日志'], ['database', '数据库连通性'], ['redis', 'Redis 连通性']]) {
      // Element Plus hides the native checkbox input; click its visible associated label.
      await environmentDialog.locator('label').filter({ hasText: new RegExp('^' + label + '$') }).click()
      assert.equal(await environmentDialog.locator(`input[type="checkbox"][value="${service}"]`).isChecked(), true)
    }
    const environmentResponse = page.waitForResponse(response => response.url().endsWith('/api/environments') && response.request().method() === 'POST')
    await environmentDialog.getByRole('button', { name: '保存配置', exact: true }).click()
    const createdEnvironment = await json(await environmentResponse)
    assert.equal(createdEnvironment.base_url, environment.base_url)
    assert.equal(createdEnvironment.model_consent, false)
    await visible(page.getByRole('heading', { name: environmentTitle, exact: true }))
    stage = 'environment_collector_check'
    const collectorCard = page.locator('.environment-card').filter({ hasText: environmentTitle })
    const collectorResponse = page.waitForResponse(response => response.url().endsWith(`/api/environments/${createdEnvironment.id}/check`) && response.request().method() === 'POST')
    await collectorCard.getByRole('button', { name: '连接与能力检查', exact: true }).click()
    const collectorCheck = await json(await collectorResponse)
    assert.ok(['success', 'error'].includes(collectorCheck.runtime_collector?.status), 'Collector returns an explicit actual status.')
    await visible(collectorCard.getByRole('heading', { name: '独立运行采集来源', exact: true }))
    if (collectorCheck.runtime_collector.status === 'error') await visible(collectorCard.getByText('采集未完成：' + collectorCheck.runtime_collector.error, { exact: true }))
    checks.push('runtime_collector_actual_result_display')
    stage = 'environment_edit'
    const environmentCard = page.locator('.environment-card').filter({ hasText: environmentTitle })
    await environmentCard.getByRole('button', { name: '编辑', exact: true }).click()
    const editEnvironmentDialog = page.getByRole('dialog')
    await formField('环境名称', environmentTitle + '-已更新', editEnvironmentDialog)
    const editEnvironmentResponse = page.waitForResponse(response => response.url().endsWith(`/api/environments/${createdEnvironment.id}`) && response.request().method() === 'PUT')
    await editEnvironmentDialog.getByRole('button', { name: '保存配置', exact: true }).click()
    assert.equal((await json(await editEnvironmentResponse)).name, environmentTitle + '-已更新')
    stage = 'environment_delete_own_qa_data'
    await environmentCard.getByRole('button', { name: '删除配置', exact: true }).click()
    const deleteEnvironmentResponse = page.waitForResponse(response => response.url().endsWith(`/api/environments/${createdEnvironment.id}`) && response.request().method() === 'DELETE')
    await page.getByRole('dialog').getByRole('button', { name: '删除配置', exact: true }).click()
    await json(await deleteEnvironmentResponse)
    assert.ok(!(await json(await page.request.get(`${origin.origin}/api/environments`))).items.some(item => item.id === createdEnvironment.id))
    checks.push('environment_create_same_authorized_origin', 'environment_edit', 'environment_delete_own_qa_data')
    // A new no-environment analyst proves denial and empty states against real authorization.
    // Its random password exists only in process memory, never in test artifacts.
    stage = 'analyst_permissions'; const analyst = { username: 'qa_' + randomUUID().slice(0, 12).replaceAll('-', ''), password: randomBytes(24).toString('base64url'), role: 'analyst', environment_ids: [] }
    await json(await page.request.post(`${origin.origin}/api/users`, { data: analyst, headers: await sessionHeaders() }))
    const deniedContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    try {
      const denied = await deniedContext.newPage()
      denied.on('pageerror', () => { browserErrors++ })
      await login(denied, analyst)
      await visible(denied.getByText('还没有工单，请先新建一张发货问题工单', { exact: true }))
      await denied.goto(`${origin.origin}/#/tickets/${ticketId}`)
      await visible(denied.getByText('权限不足', { exact: true }))
      await denied.getByRole('button', { name: /案例库/ }).click()
      await visible(denied.getByRole('heading', { name: '可审核案例库', exact: true }))
      assert.equal(await denied.getByRole('button', { name: '审核案例', exact: true }).count(), 0)
      await denied.getByRole('button', { name: /环境配置/ }).click()
      await visible(denied.getByText('没有可访问的环境，请由管理员添加配置并分配访问权限', { exact: true }))
      assert.equal(await denied.getByRole('button', { name: '新增环境', exact: true }).isDisabled(), true)
      await denied.screenshot({ path: resolve(output, 'analyst-empty.png'), fullPage: true })
      checks.push('real_403', 'real_empty', 'analyst_controls')
    } finally { await deniedContext.close() }
    assert.equal(browserErrors, 0, 'Unexpected browser runtime errors occurred.')
    const report = { scope: 'isolated_local_erp_lab', mocks: false, browser: { channel, version: browser.version() }, browser_errors: browserErrors, checks, ticket_id: ticketId, delivery_note: deliveryNote, diagnostic_state: diagnostic.task.state, review_state: reviewed.task.state, business_complete: reviewed.task.result?.review?.business_complete === true, manual_override: closedTicket.manual_override === true, diagnosis_metrics: diagnostic.task.result?.metrics, collector_status: collectorCheck.runtime_collector.status, collector_error: collectorCheck.runtime_collector.error, elapsed_seconds: (performance.now() - started) / 1000, artifacts: ['diagnosis.png', 'case.png', 'environment.png', 'analyst-empty.png'], note: 'Component tests separately cover loading/interrupted/error states with explicitly mocked HTTP. This run did not mutate ERP data. QA analyst has no environment grants. Only the case and duplicate environment created by this run were deleted. Collector UI accepts explicit error as a correctly displayed degraded state; see collector_status rather than inferring collector availability from UI test success.' }
    await writeFile(resolve(output, 'result.json'), JSON.stringify(report, null, 2), { flag: 'wx' })
    console.log(JSON.stringify({ result: 'passed', checks: checks.length, report: resolve(output, 'result.json') }))
  }
} catch (error) {
  // Avoid raw Playwright call logs: some input failures can include filled secrets.
  const failure = { result: 'failed', stage, error_type: error.name, locations: [...String(error.stack || '').matchAll(/workbench\.mjs:\d+:\d+/g)].map(match => match[0]), completed_checks: checks, elapsed_seconds: (performance.now() - started) / 1000 }
  if (!args.includes('--probe')) { await mkdir(output, { recursive: true }); await writeFile(resolve(output, 'failure.json'), JSON.stringify(failure, null, 2), { flag: 'wx' }); failure.report = resolve(output, 'failure.json') }
  console.error(JSON.stringify(failure))
  process.exitCode = 1
} finally { await context.close(); await browser.close() }
