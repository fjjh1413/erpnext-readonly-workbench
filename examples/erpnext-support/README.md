# ERPNext 库存与发货异常诊断工作台

本目录是基于 HolmesGPT 的新增 **单企业内部多账号**库存售后工作台。当前 v2 使用 PostgreSQL / SQLAlchemy / Alembic 持久化、Celery / Valkey 独立任务队列、Vue 3 严格 TypeScript / Element Plus 页面、FastAPI、nginx 和可选 Prometheus。ERP 始终只读，工作台只修改自己的账号、工单、案例与人工记录；原 ToolExecutor / ToolCallingLLM 和 ERP 工具链继续复用。

**v2 新部署入口为 `http://127.0.0.1:8875`**，步骤见 [企业部署说明](enterprise/README.md) 和 [升级与兼容边界](ENTERPRISE_UPGRADE.md)。原 SQLite / 8765 / 四页操作与下方首版安装步骤保留为 v1 历史说明；不能直接用新代码原地打开旧 schema。旧数据必须显式以只读复制方式迁入另一个空目标，源文件和既有 runtime 不覆盖。

当前只验证 **ERPNext 15.85.1 / Frappe 15.87.0**。ERP 官方镜像使用自身的 Python 3.11；工作台与 HolmesGPT 使用独立 Python **3.12.10**。实验数据库是 MariaDB 10.6.22，不能用原首版 MySQL 示例替代 ERPNext 数据库。

这是授权测试/试点版本，没有客户或生产审核；v2 还没有 OIDC、MFA 或 HA。v2 实际结果见 [企业升级测试报告](ENTERPRISE_TEST_REPORT.md)，首版历史结果保留在 [TEST_REPORT.md](TEST_REPORT.md)，跨轮进度见 [PROGRESS.md](PROGRESS.md)。没有模型凭据时返回明确的 `rules_degraded`；原引擎的自动调查循环已用可控模型测试，但尚未调用真实模型。企业队列的 `.invalid` 不可用数据源测试只验证调度、失败留痕与降级，不能称为真实 ERP 效果。

## v1 历史本机打开方式

- 工作台：[http://127.0.0.1:8765](http://127.0.0.1:8765)
- 隔离 ERP：[http://127.0.0.1:8090](http://127.0.0.1:8090)
- 工作台初始账号在 `.runtime/workbench/initial-login.json`，随机密码仅存在本机受限文件；不要发到聊天或 Git。
- 原 Docker 首版和其他既有容器保留。此模块不会接管它们。

ERP 是业务系统，8765 是新增售后工作台；服务启动耗时与业务任务耗时分别记录。访问 `/health` 仅证明工作台进程可用，不证明 ERP 业务完成。

## v1 安装记录与 v2 兼容安装说明

v2 推荐按 [enterprise/README.md](enterprise/README.md) 创建新随机项目，镜像构建保留主 Poetry 锁，企业依赖仅从 `enterprise/requirements.txt` 补充固定版本；前端依赖由 `frontend/package-lock.json` 固定，`npm run build` 先执行严格 `vue-tsc` 检查。新部署目录、数据库、队列和 8875 网关都与旧项目分开，不能让以下旧 `run.ps1` 接管它们。

若只在新的专用 Python 3.12 环境做本机开发，安装原主锁依赖后还需显式安装企业扩展依赖：`python -m pip install -r examples/erpnext-support/enterprise/requirements.txt`。不要为此无差别升级主依赖、重写 `poetry.lock` 或覆盖已有环境。SQLAlchemy 复用主锁中的现有版本；SQLite 仅用于新的本机测试 schema，旧数据复制迁移见 [持久层说明](../../holmes/erpnext_support/persistence/README.md)。

以下步骤是 v1 历史复现记录；旧环境和文件已有时只能检查，不代表已经转换为 v2。

先检查 Git 状态并保留现有文件。已有 `.venv`、runtime 和配置时直接检查，不覆盖、重建或升级它们。

```powershell
Set-Location E:\ai\holmesgpt
git status --short
py -3.12 --version
docker version
docker compose version
node --version
```

仅在 `.venv` 不存在时创建环境。Poetry 使用原仓库锁，不重新解析/升级主依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install 'poetry==1.8.5'
$env:POETRY_VIRTUALENVS_CREATE = 'false'
.\.venv\Scripts\poetry.exe install --only main --no-root
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

本次实际测试使用 pytest 9.1.1，上游 dev 锁为8.4.2；仅装上游dev锁不会得到同一测试版本。新建的专用测试环境可用 `python -m pip install -r tests/erpnext_support/requirements-test.txt` 复现本次pytest版本；它不改主锁、不升级运行依赖。已有环境先检查版本再决定，不覆盖现有 `.venv`。前端测试版本在 `frontend/package-lock.json`。

```powershell
Set-Location E:\ai\holmesgpt\examples\erpnext-support\frontend
npm ci
npm run build
Set-Location E:\ai\holmesgpt
```

ERP 实验环境启动和故障脚本见 [lab/README.md](lab/README.md)。先创建全新随机项目，再启动；配置和拥有的资源 ID 均保存于 `lab/.runtime/<project>/`。已有 8090 监听时应检查当前实验，不启动第二个占用端口的项目。

首次初始化工作台使用：

```powershell
.\examples\erpnext-support\run.ps1 -Action Init -ErpUrl http://127.0.0.1:8090
.\examples\erpnext-support\run.ps1 -Action Start
.\examples\erpnext-support\run.ps1 -Action Check
```

`Init` 拒绝覆盖已有 runtime。`Start` 拒绝占用中的 8765，生成独立进程记录；首次导入 HolmesGPT 依赖可能约一分钟。也可以在自己的终端直接运行：

```powershell
.\.venv\Scripts\python.exe -m holmes.erpnext_support serve --port 8765
```

实验只读凭据可用专用接入脚本绑定。`$taskERPConfig` 必须是 `lab.py create` 返回的本实验路径；脚本拒绝不同的已有凭据及重复环境登记：

```powershell
.\.venv\Scripts\python.exe .\examples\erpnext-support\connect-demo.py --lab-config $taskERPConfig
```

需要运行层证据时，在独立终端启动受限采集器。它仅绑定本机 8767，检查此实验的拥有记录、容器和网络标签；HTTP 只提供固定 `GET /snapshot`，没有 Docker 命令参数。采集器凭据不能放进网页。

```powershell
.\.venv\Scripts\python.exe -m holmes.erpnext_support.collector --lab-config $taskERPConfig --runtime examples/erpnext-support/.runtime/collector
# 尚未登记环境时可添加 --collector；已有环境用环境页明确编辑。
.\.venv\Scripts\python.exe .\examples\erpnext-support\connect-demo.py --lab-config $taskERPConfig --collector
```

已登记环境要先在服务端将本采集器 `collector.json` 安全配置成 `.runtime/workbench/collectors/erp-lab.json`，再在环境页填写该别名并选择允许的服务。当前采集器只适配隔离实验；客户运行层需审核并实现客户的受限采集接入，不能直接套用实验拥有记录。

## 客户配置与模型

[deploy/](deploy/) 是独立客户工作台容器模板，使用客户自行维护的 HTTPS ERP；[lab/](lab/) 是会初始化业务数据的实验室，二者用途不同。客户模板不初始化/写入客户 ERP，不挂载 Docker socket 或 ERP 数据库卷。

服务端 `settings.json` 的 `allowed_erp_origins` 是明确的地址白名单；环境页只登记白名单内 origin 和服务端凭据别名。每个 `<别名>.json` 只含 `api_key`、`api_secret`，保存在 runtime/credentials，受本机 ACL 限制。不要将密钥放在 URL、前端字段或启动参数中。客户只读扩展部署及权限必须经客户授权管理员审核；附带实验脚本不允许操作客户环境。

登录后可在 `/api/users` 创建 analyst/reviewer 及环境授权。admin 管理环境和授权；analyst 可调查、记录动作、编辑本人未审核案例；reviewer/admin 审核案例及确认关闭。初始随机 admin 应仅由部署管理员使用，不作为共享售后账号。会话为 HttpOnly Cookie，修改请求另需 CSRF；无环境授权的账号不能读取工单、证据或调用该环境工具。

模型配置需同时满足：服务端明确 `model` 与 `model_api_base`、进程环境变量 `ERP_SUPPORT_MODEL_API_KEY`、对应环境 `model_consent=true`。不要把 `.env.example` 中的占位符当成真实配置。默认不读个人 Holmes 配置、不自动寻找其他 Key；目标记录在诊断结果中。授权外部模型会接收必要且脱敏的业务证据，私有工作台不等于数据不出网。

默认最多4轮、30次工具调用、总预算90秒、每工具8秒、模型输出2048 token、50行、10个发货明细，证据保留30天。设置必须在服务端允许的范围内；达到预算、重复无新增查询、权限撤销或两次失败时停止调查，并保留本轮已经执行的并行调用记录。没有实际计费信息时不估算费用。

## 支持的判断与限制

| 类别 | 判断范围 |
|---|---|
| 实际缺货 | 普通草稿出库、完整可读明细、标准库存语义、禁止负库存时，根据原生权威查询确认；截断或负库存设置不明时降为候选 |
| 历史时点不足 | 比较该单过账时点的权威余额与当前余额，并用入库流水支持时间关系；不手写流水求和 |
| 预留/关联冲突 | 可见关联销售单、有效预留与仓库关系的候选；不可见记录与完整分配语义明确缺失 |
| 批次/序列号 | 读取批次包元数据；完整历史批次可用量、自动分配与序列冲突尚未适配，不作确定根因 |
| 单据/权限 | 草稿、取消、关联前置条件；诊断账号读取拒绝单独分类，故障操作用户提交权限仍需补证 |
| 运行层 | 授权服务状态、MariaDB原镜像健康检查、Redis固定PING及有界应用日志；状态异常仅是业务故障候选 |
| 已审核案例 | 仅审核通过且固定版本一致的参考；还需满足必要证据与条件，不把版本匹配当作缺陷确认 |

未适配扩展、库存维度、自定义工作流、库存重算、时区无法核验或调查中单据变化会限制结论。接口可用、容器运行、单据状态变化均不能自动证明发货完成或原候选原因正确。业务完成复核必须读取已提交单据及对应未取消的实际出库流水；关闭始终由人确认，未完整复核的例外关闭须显式说明依据。

## 按步骤演示

1. 登录8765，打开“环境配置”，检查实际版本、各业务对象读取权限和独立运行层来源；错误/缺权限也会显示，不能只看连接状态。
2. 新建工单，选择实验环境，输入 `seed-result.json` 的真实发货单号及含时区的发生时间。
3. 正常单、缺货单、历史不足单、普通草稿分别执行诊断；展开证据，检查查询、采集时间、业务时点、调用状态与编号。
4. 缺货案例输出处理建议，工作台不会补货。实验操作者可执行 `lab.py manual_recheck` 生成**独立**人工处理样例，原四案例保留。
5. 在工单记录实际人工动作，点击复核；复核会重新请求 ERP，观察出库流水与单据是否齐全。已提交正常单也可演示重新核对并由人关闭，不将此步骤称为修复缺货。
6. 在案例库创建草稿、审核、编辑；编辑会撤销原审核。未审核或其他版本的案例不进入后续参考。
7. 停止工作台再启动时，未完成任务标记“中断”，已保存步骤保留，可重新执行；不会永久停在调查中。

自动浏览器脚本见 `frontend/e2e/workbench.mjs`，只允许本机工作台和8090实验 ERP，使用新的 headless 浏览器上下文。测试不保存登录会话、HAR 或 trace。

## 维护、备份和回退

```powershell
.\examples\erpnext-support\run.ps1 -Action Check
.\examples\erpnext-support\run.ps1 -Action Backup -OutputPath 'E:\ai\backups\workbench-NEW.sqlite3'
.\examples\erpnext-support\run.ps1 -Action Stop
.\.venv\Scripts\python.exe -m holmes.erpnext_support restore --backup 'E:\ai\backups\workbench-NEW.sqlite3' --output 'E:\ai\backups\restored-NEW.sqlite3'
```

备份使用 SQLite backup API，并校验恢复源完整性；只恢复到新文件，拒绝覆盖运行数据库。凭据、设置、账号初始文件要由部署管理员另外加密备份；工作台备份不包含 ERP 数据库。关闭后校验新库、显式保留旧库并由管理员替换运行库，再启动和检查。恢复后的会话仍可能有效，客户流程应在受控恢复时清理会话并重新登录。

升级前保存当前镜像摘要/代码提交、锁文件和工作台备份；在独立目录/端口运行新版本和只读验证，再切换。失败时停新工作台，回到保存的旧镜像及它的兼容备份；不使用 `git reset --hard`、覆盖用户配置或回滚客户 ERP 数据库。ERP 升级改变版本后本适配器会明确拒绝确认库存根因，需先重新审查和测试。

证据在启动时及每小时按保留期限清理完整结果与步骤；工单、人工动作和审计元数据保留。已导出的报告、备份及实验日志不由该策略自动删除，需部署管理员单独制定留存策略。脱敏不是匿名化保证；本机 ACL、TLS、角色、最小权限与客户审核仍需一起落实。

## 上游与新增工作

| 上游或既有功能 | 本次新增 |
|---|---|
| 原 Tool/Toolset、ToolExecutor、ToolCallingLLM.call_stream、LLM配置与调用结果 | `holmes/plugins/toolsets/erpnext_support`业务适配与固定版本规则；原执行链路的集成测试 |
| 首版 Docker只读工具、脱敏及有界本地执行 | `holmes/erpnext_support`工单API、任务存储、角色/授权、受限ERP实验collector |
| 原 Holmes CLI/server和上游测试 | 独立库存售后工作台，不改上游服务入口或核心Agent源码 |
| 原项目许可证/主Python锁 | Vue四页、独立frontend锁、ERP实验Compose、客户工作台模板、中文文档与分层实测 |

既有首版 `docker_readonly/security.py` 仅调整两处脱敏正则的起始边界，修复长输入反复扫描；其4项既有focused测试实际通过。其余上游核心Agent、许可证、主Python依赖与锁文件均保留。

HolmesGPT 原根 `LICENSE` 为 Apache-2.0，未修改。ERPNext GPL-3.0、Frappe MIT、Vue/Element Plus MIT、MariaDB/Redis/基础镜像及其他依赖各自遵循其实际上游许可；分发镜像前需保留各层通知并核查分发方式，不能统一称为 Holmes 的 Apache 许可。版本源码与权限审核见 [SOURCE_AUDIT.md](../../holmes/plugins/toolsets/erpnext_support/SOURCE_AUDIT.md) 和 [lab/VERSIONS.md](lab/VERSIONS.md)。
