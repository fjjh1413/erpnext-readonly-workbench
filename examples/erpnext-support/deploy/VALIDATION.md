2026-10-06 本机实际执行记录（JSON 时间使用 UTC，因此文件名可能为 2026-10-05）。

- 固定镜像工具链拉取成功：Node 24.21.0、Python 3.12.10。
- 原始 `poetry.lock` 的 177 个主依赖安装成功，Poetry 1.8.5 未重写锁文件。
- 前端通过 `npm ci` 使用 package-lock，实际完成 Vite 构建。
- 首次配置包含 frontend 字符串，导致页面失败；保留 [失败说明](INITIAL_FAILURE.md)。
- 全新随机运行目录复跑：健康、随机登录、会话、构建页面、空客户环境均真实通过。
  [容器实际报告](container-validation-20261005T162413799487Z.json)记录 116.497 秒（包含容器启动）。
  镜像 `holmes-erp-workbench:20261006-daaee94e8be0`；此报告对应当时源快照，不代表后续改动。
- 容器 UID/GID 为 10001:10001、根文件系统只读、只有私有状态绑定目录、无 Docker socket。
- 原有 4 个 FastAPI 实验容器与 3 个 nginx 的 ID 和状态相同。
- 独立真实 Edge 浏览器验证登录、工单空态、未选工单详情、案例空态、环境空态、退出共 6 项通过。
  浏览器报告由前端审查模块保存于 `frontend/.qa/deployment-2026-10-05T16-28-04.041Z-a951a1ef/result.json`，
  可分享的脱敏报告由前端模块导出至根 `reports/`。
- 部署准备模块单元测试 9/9 通过；ERP 实验模块权限和生命周期单元测试另有 43/43 通过。

最终冻结源镜像为 `holmes-erp-workbench:20261006-ad26c4235286`，实际 digest 为
`sha256:17afe00d629745bae12815a5e91048ce269f0efeb25618caaec52a979bd3231f`。
[构建和源码核对报告](build-validation-20261006-ad26c4235286.json)记录
[完整源文件清单](build-source-manifest-20261006-ad26c4235286.json)的 SHA256：
`55a7f78e53cddfa9aaf2e29b1b2159526c2947948695f8705d55eaddc1e141cf`。
291 个构建输入的路径及哈希与当前源码完全一致，包括全部 Holmes 源文件、前端源码和两个依赖锁。
Node/npm 和 Poetry 依赖层使用前次真实成功构建的缓存，最终构建退出码为 0。

[最终新运行目录容器报告](container-validation-20261005T165312215043Z.json)实际用时
89.177 秒（从容器启动开始计时）：Docker 健康状态为 healthy，健康接口、随机登录、会话、
前端 HTML、实际两份编译后 JS/CSS 资源与客户环境空态均通过。
容器还核对了 cap_drop ALL、no-new-privileges、唯一私有状态绑定和独占实验网络。
只清理本次创建的临时容器与网络，镜像、新私有运行目录与旧验证报告全部保留。

本容器检查 ERP 请求数为 0、未调用模型，不能称为容器 ERP 联调或真实模型准确率测试。
宿主版与真实 ERP 的业务/权限/人工复核测试记录位于 `lab/reports/` 与根 `reports/`。
本机演示 ERP 保持运行，临时 8766 容器验证完后只按自己记录的 ID/标签清理；私有运行目录保留。
