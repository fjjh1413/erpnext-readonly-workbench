# ERPNext 售后诊断工作台开发进度

本次请求：固定 ERPNext 版本线，库存与 Delivery Note 发货诊断，单客户私有部署；只读 ERP，允许保存工作台自己的工单和记录。

## 已确认的基线

- 上游 origin 为 https://github.com/HolmesGPT/holmesgpt；本地首版提交 `14fe882f45fe1681c30196e534a9d42558397ffc`。
- 首版有 5 个只读 Docker 工具、规则报告、原 Tool/ToolExecutor 接线和可选 ToolCallingLLM；没有前端和工单存储。
- 首版实际报告及用户未追踪的 `manual-20261005-221848.json` 保留；当前 7 个已有容器不接管、不删除。
- Python 3.12.10 独立 `.venv`；Node 24.21.0；Docker Engine 29.8.1 / Compose 5.5.1，Linux Engine 8 CPU、约 6GB 内存。

## 已实际完成（2026-10-06 北京时间）

1. 官方镜像真实安装：ERPNext15.85.1/Frappe15.87.0、MariaDB10.6.22、Redis6.2.18；新项目 `holmes-erp-07dff38559e8`，只发布回环8090，原7容器仍运行。
2. 五个ERP业务工具、固定端点/子行范围、权限检查、原生历史库存、结构化证据和规则；原 Tool/ToolExecutor/ToolCallingLLM 复用，源码审核未复制ERP实现。
3. FastAPI/SQLite登录、角色、每次环境授权、持久任务、重启中断、人工处理/重新读取/人工关闭、案例审核及编辑撤审。
4. Vue3/Element Plus四个中文页面和同源生产bundle；32明确Mock组件测试，21真实ERP页面检查通过72.00秒，真实7/7调用成功8.585秒；模型未调用。
5. 真实ERP12场景通过66.24秒；正常、实际缺货、历史不足、状态前置、权限拒绝、缺日志降级、独立人工补货后实际出库复核。
6. 独立受限运行collector：固定GET、拥有记录/标签/网络验证、有界投影/日志、固定RedisPING；5真实运行测试通过235.86秒，DB/cache/backend停止均有证据候选并恢复ready。
7. 工作台43单元/API测试通过100.28秒；修复任务完成发布及同快照详情读取。CLI实际备份恢复新文件通过。
8. Docker客户模板实际构建/独立8766容器登录和四页空态通过，非root/只读root/无socket/私有state挂载；冻结源码最新镜像 `holmes-erp-workbench:20261006-ad26c4235286` 实际smoke通过89.177秒，JS/CSS资源也200。

## 最终核验

- 慢HTTP硬期限、采集schema、证据内容对应实际调用、HMAC标识避免归组冲突及长日志脱敏性能均已修复；147项适配/规则/原循环/安全回归通过45.48秒。
- 最终安全源码实际ERP正常/缺货补验2项通过48.22秒；已重载本机服务，HTTP工单实际诊断7/7成功9.569秒，单据及实际流水齐全，仍明确partial/rules_degraded。
- 120个交付文件与13个已生成实验/工作台/容器秘密扫描0命中；根许可证、pyproject与poetry.lock未改变，用户manual报告未加入提交。
- 实际失败报告保留，最终结果见 TEST_REPORT.md 与 reports；本地签名提交后只保留用户原有未追踪报告。

## 后续需要外部输入的验收

在授权管理员配置模型目标及Key、环境明确授权后，运行同一案例组的真实模型/上游未适配对照和供应商用量验证；不把现有Mock循环证明替代这项评测。客户试点前另行审核自己的扩展、权限、TLS/账号、运行采集源与许可分发，并补实测，不在本机直接连接客户或生产。

## 未执行/明确限制

- 未调用真实外部模型，未获取模型用量/计费；原循环ScriptedMockLLM仅证明接线和约束。缺Key只阻塞这部分。
- 没有未适配Holmes/真实模型对照或客户效果数据，没有生产/客户审核。
- 完整历史批次/序列号分配、定制工作流/库存维度、当前版本真实软件缺陷复现与客户运行collector未验收，均明确限制，不猜测。
- 演示ERP与客户模板分开；已有本机服务与文件保持，工作台只写自己的SQLite，Agent从未提交/修改ERP或重启服务。

## 企业版 v2（2026-10-10，北京时间）

升级目标已按用户确认采用单企业内部、多账号协作。复用原 Holmes 工具执行链路、ERP 只读适配器、规则和工作台，不新增 ERP 写工具。工作台自己的持久层、调度、网页与部署完成企业版升级。

- 当前网页：`http://127.0.0.1:8875`，独立项目 `holmes-enterprise-c63b69b8be5f`。
- 当前管理配置：`enterprise/.runtime/holmes-enterprise-c63b69b8be5f/config-version-7fd288c93ab6.json`。停止/检查/后续更新必须使用这份新版本配置，原 `config.json` 保留用于回溯。
- 当前初始登录文件：`enterprise/.runtime/holmes-enterprise-c63b69b8be5f/state/initial-login.json`。随机密码保存在私有文件，不写进文档或 Git。
- 当前镜像：`holmes-erp-enterprise:20261010-39edc000881c`，manifest `sha256:6863aa62c947b0bba735fb5c87acad3116a151041154f51c3a928d087908eb8a`。
- 技术栈：Python 3.12/FastAPI、Vue 3/Element Plus/严格 TypeScript、PostgreSQL 17.11/SQLAlchemy 2/Alembic、Celery 5.6.3/Valkey 8.1.10、nginx 1.30.5、Prometheus 3.13.4 LTS。
- 新增：共享账号与会话、角色和环境授权、撤销会话、数据库分页、追加审计与请求编号、数据库租约/持久待发队列/重复任务保护、独立 Worker、最小数据库权限、内部监控及受控备份恢复/镜像更新。
- 实测：核心/API/jobs 68 项，原链路/规则/运行工具 89 项；跨数据库存储组合 64 项，审计/指标定向同 7 场景分别在 SQLite/真实 PostgreSQL 各通过；前端 49 mock 测试、真实浏览器 23 检查点；部署策略 52 mock 测试、真实部署 19、备份恢复 9、保留数据更新 12 检查点。不同轮次存在重叠，完整结果不累计为独立场景总数。
- 实际队列故障 12/12 通过：断连时任务持久化，恢复后 13.206 秒消费同一任务；不可用数据源诊断 2.350 秒、4 次调用/0%成功，明确部分结果及缺失信息。Worker 中断最终 12/12 通过：保留 1 条完成的 Mock 调用，旧任务正确中断，测试控制器显式恢复同一 Worker，59.771 秒重新就绪；诊断 Agent 没有自动修复能力。
- 分模块签名本地提交：`cb7716da9` 企业后端、`e51bd9cfd` 实栈发现的审计/指标修复、`d554bfe74` TypeScript/账号/审计网页；部署、演练与中文交付的提交可通过当前分支 `codex/enterprise-workbench-v2` 的 `git log` 核对。未推送。

旧许可证、主 `pyproject.toml`/`poetry.lock`、旧运行目录与用户原有 manual 报告保留；1547 个已有 v1 运行文件通过哈希核对。企业版使用新 PostgreSQL 库，没有自动导入用户旧 SQLite；显式只读复制能力已在合成旧库与全新测试 PostgreSQL 中验证。

当前验证没有真实模型、没有重新执行真实 ERP v1 场景，也没有 OIDC/MFA、跨机高可用或生产规模压测。故障控制器仅中断/恢复所属隔离测试容器，诊断 Agent 不执行 ERP 或基础设施自动修复。完整实际结果、失败与边界见 [ENTERPRISE_TEST_REPORT.md](ENTERPRISE_TEST_REPORT.md)，中文启动/检查/停止/升级/备份恢复见 [enterprise/README.md](enterprise/README.md)。
