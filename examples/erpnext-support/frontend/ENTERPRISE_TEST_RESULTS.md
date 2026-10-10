# 企业前端升级实际验证记录

日期：2026 年 10 月 10 日（本机 Asia/Shanghai）。原首版历史记录保持在 [TEST_RESULTS.md](TEST_RESULTS.md)，不计入此次升级的结果。

## 严格类型检查与构建

实际执行 `npm run typecheck` 成功；生产 `npm run build` 包含同样的严格 Vue / TypeScript 类型检查，最新构建成功，Vite 阶段 4.34 秒，转换 1604 个模块。JavaScript 1062.82 kB（gzip 338.26 kB），CSS 367.21 kB（gzip 49.95 kB）。完整 Element Plus 导入仍有大于 500 kB 的构建提示，此限制保留如实报告。

业务源文件及 Vue 页面开启 `strict`，没有使用 `@ts-ignore` 关闭检查；显式模拟测试和浏览器测试工具脚本保持 JavaScript。类型检查不是接口权限或真实 ERP 可用性验证。

## 显式模拟组件及 API 测试

最新完整执行：**6 个文件、49 项通过、0 项失败，耗时 86.84 秒**。测试使用显式 HTTP mock，不连接 ERP、数据库、任务队列或模型。

| 文件 | 数量 | 主要验证 |
| --- | ---: | --- |
| `src/api.test.js` | 15 | 同源 Cookie/CSRF、请求编号及会话撤销保留编号、错误、分页参数、账号修改与独立密码接口 |
| `src/result.test.js` | 6 | 报告映射、证据与缺失信息、最新复核和关闭条件 |
| `src/components/StatePanel.test.js` | 4 | 加载、空态、权限失败、重试 |
| `src/pages/DetailPage.test.js` | 5 | 部分完成、中断、证据文本安全、异步任务与人工关闭 |
| `src/pages/Workspaces.test.js` | 9 | 原工单/案例/环境表单、筛选及角色边界 |
| `src/pages/EnterprisePages.test.js` | 10 | 账号角色/授权、密码清空、审计分页/403/转义、真实状态字段展示 |

首次全量运行有 39 项通过，但剩余工作区文件因 Vitest worker 启动超时未执行，退出码 1；没有将这次运行记为成功。之后该文件独立执行 9 项通过，再完整重跑 48 项全部通过。补充会话撤销编号回归后，API 文件 15 项定向通过，最终上述 49 项完整通过。保留原单线程隔离配置，没有跳过测试或放宽超时来隐藏错误。

完整原始报告仅保存在忽略目录 `.qa/enterprise-components-83352d12b2db.json`（失败）、`.qa/enterprise-components-8a4c66d1873d.json`（48 项通过）及 `.qa/enterprise-final-7c4eb7c31ca1.json`（49 项通过）；不含真实账号凭据。可交付的精简摘要在 `reports/`，不复制 mock 输入内容。

## 真实企业页面联调

企业实例 `127.0.0.1:8875` 首轮真实浏览器执行失败：17.998 秒内完成 12 个账号创建、角色/环境授权、密码重置、403 请求编号、停用及会话撤销检查，随后审计查询断言失败。源码复核确认服务端关键词查询当时只检索请求编号、账号和操作名，未覆盖页面提供的资源编号检索；查询本轮新建账号编号得到空列表。这次退出码为 1，不能记为完整 UI 验收通过。

失败报告安全导出至 [reports/enterprise-ui-initial-failure-20261010.json](reports/enterprise-ui-initial-failure-20261010.json)，原始报告保持在 `.qa/enterprise-2026-10-10T08-18-35.954Z-e4e30638/failure.json`。该轮新建 QA 账号已实际停用并清空环境授权；尚未创建诊断工单，也没有调用 ERP 或模型。服务端补齐资源编号、资源类型和已脱敏详情检索并实际升级后，保留原资源检索断言重新验证，没有放宽断言隐藏接口缺陷。

升级后的第二轮在 21.088 秒完成 16 个检查，真实资源检索、审计详情、接口分页及系统状态已通过，随后旧四页导航处失败。原因属于 **测试定位错误**：未限定导航范围的按钮名称正则同时匹配侧栏“工单工作台”和详情页“返回工单工作台”。仅在 E2E 中把定位限定到 `工作台导航` landmark，业务 `src` 和 `dist` 未修改。此轮同样退出 1，QA 账号已停用并清空授权，未启动诊断任务。安全摘要在 [reports/enterprise-ui-navigation-first-failure-20261010.json](reports/enterprise-ui-navigation-first-failure-20261010.json)，原始失败报告保持在 `.qa/enterprise-2026-10-10T08-52-35.165Z-3bcb9ed6/failure.json`。

最终完整复测实际通过：**23/23 个同一浏览器流程检查点，31.528 秒，退出码 0，0 个页面错误**。使用 Edge 154.0.4258.62 的全新隔离上下文，HTTP/API 没有 mock，实际连接新企业栈的 PostgreSQL、Celery / Valkey 和独立 worker。检查包括账号创建、角色/环境授权、密码重置、审核员权限拒绝、停用/会话撤销、登录错误请求编号、审计资源检索与脱敏详情、真实接口 offset 分页、系统状态、原四页，以及新 QA 工单的异步诊断、展开证据、人工记录、重新复核和明确的人工例外关闭。

| 最终真实浏览器中的诊断结果 | 实际值 |
| --- | --- |
| 数据源 | 明确的 `.invalid` 不可用测试源；未连接真实 ERP |
| 任务与模式 | `partial` / `rules_degraded`，分类 `runtime_unavailable` |
| Agent 采证耗时 | 1.215224 秒；不等于整段浏览器流程或任务排队耗时 |
| 工具记录与证据 | 4 次调用、4 项带编号的失败证据，调用成功 0/4（0%） |
| 业务复核 | `partial`，`business_complete=false` |
| 最终处理 | 人工记录后重新读取，再以显式 `manual_override=true` 及说明关闭本轮新工单 |
| 模型 | 未调用；模型字段为 `null` |

不可用源的 0% 调用成功率如实保留；浏览器通过证明调度、失败留痕和人工协作链路工作，不证明 ERP 内部根因已经确认、库存已修复或模型效果通过。新建测试账号最终已停用且环境授权为空。23 个检查点不能当作 23 个独立故障场景，也不能与另一个 API 烟测的耗时相加。

最终安全报告：[reports/enterprise-ui-final-20261010.json](reports/enterprise-ui-final-20261010.json)，原始报告保持在 `.qa/enterprise-2026-10-10T08-54-03.636Z-2d5d91da/result.json`。三个原始 PNG 已逐张实际查看，没有可见密码、令牌或 Cookie，按原字节导出，未编辑图片：[企业账号](reports/enterprise-ui-users-20261010.png)、[审计与状态](reports/enterprise-ui-audit-20261010.png)、[部分完成的诊断](reports/enterprise-ui-fixture-partial-20261010.png)。账号截图采自密码重置后、停用前；诊断截图采自人工记录/复核/关闭前，最终状态由 JSON 的实际接口断言证明，不将截图冒充关闭后的画面。

49 个显式模拟测试不能替代 PostgreSQL、分布式队列、真实权限撤销或审计持久化的完整联调。浏览器脚本仅导出安全 JSON 和登录后非敏感截图，不记录 Cookie、密码、HAR、trace 或 storageState。

本机浏览器启动探测已实际通过：Microsoft Edge 154.0.4258.62，全新隔离上下文，2.507 秒；没有连接日常浏览器配置。启动探测不等于页面联调通过。

本次前端没有调用真实模型，也没有执行 ERP 业务写操作；首版 21 个浏览器检查点与 12 个真实 ERP 测试等历史证据不能替代本次升级验证。
