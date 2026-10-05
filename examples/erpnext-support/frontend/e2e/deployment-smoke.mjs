// Real isolated deployment UI smoke; no ERP connection or API mocks.
import assert from 'node:assert/strict'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import { chromium } from 'playwright'
const args = process.argv.slice(2)
const option = name => args[args.indexOf(name) + 1]
assert.ok(args.includes('--url') && args.includes('--credentials'), 'Explicit local deployment and credentials path required.')
const origin = new URL(option('--url'))
assert.equal(origin.hostname, '127.0.0.1')
assert.equal(origin.protocol, 'http:')
assert.equal(origin.pathname, '/')
const directory = resolve('.qa', 'deployment-' + new Date().toISOString().replaceAll(':', '-') + '-' + randomUUID().slice(0, 8))
const started = Date.now()
const checks = []
let browser, stage = 'launch'
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  let errors = 0
  page.on('pageerror', () => { errors++ })
  const account = JSON.parse(await readFile(resolve(option('--credentials')), 'utf8'))
  stage = 'login'
  await page.goto(origin.origin)
  await page.locator('input[autocomplete="username"]').fill(account.username)
  await page.locator('input[autocomplete="current-password"]').fill(account.password)
  await page.getByRole('button', { name: '登录工作台', exact: true }).click()
  await page.getByRole('heading', { name: '工单工作台', exact: true }).waitFor()
  await page.getByText('还没有工单，请先新建一张发货问题工单', { exact: true }).waitFor()
  checks.push('real_container_login', 'tickets_empty')
  await mkdir(directory, { recursive: true })
  await page.screenshot({ path: resolve(directory, 'deployment-tickets.png'), fullPage: true })
  for (const [navigation, heading, empty] of [
    ['诊断详情', '诊断详情', '请先从工单工作台选择一张工单'],
    ['案例库', '可审核案例库', '没有符合条件的案例，创建案例或调整筛选'],
    ['环境配置', '环境与只读能力', '没有可访问的环境，请由管理员添加配置并分配访问权限'],
  ]) {
    stage = navigation
    await page.getByRole('button', { name: new RegExp(navigation) }).click()
    await page.getByRole('heading', { name: heading, exact: true }).waitFor()
    await page.getByText(empty, { exact: true }).waitFor()
    checks.push(navigation + '_real_empty')
  }
  assert.equal(errors, 0)
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await page.getByRole('button', { name: '登录工作台', exact: true }).waitFor()
  checks.push('real_logout')
  const report = { result: 'passed', scope: 'fresh_isolated_container_without_erp_environment', url: origin.origin, mocks: false, browser: { channel: 'msedge', version: browser.version() }, browser_errors: errors, checks, elapsed_seconds: (Date.now() - started) / 1000, screenshot: 'deployment-tickets.png', note: 'No ERP requests, traces, saved sessions or credentials were exported.' }
  await writeFile(resolve(directory, 'result.json'), JSON.stringify(report, null, 2), { flag: 'wx' })
  console.log(JSON.stringify({ result: 'passed', checks: checks.length, report: resolve(directory, 'result.json') }))
  await context.close()
} catch (error) {
  console.error(JSON.stringify({ result: 'failed', stage, error_type: error.name, completed_checks: checks }))
  process.exitCode = 1
} finally { if (browser) await browser.close() }
