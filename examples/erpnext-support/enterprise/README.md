# 单企业内部多账号部署

该模块复用 Holmes 诊断链路与现有 ERPNext 只读工具，新增 PostgreSQL 持久存储、
Valkey/Celery 队列、独立 worker、nginx 网关和可选 Prometheus。工作台可写自己的账号、
工单和诊断任务；ERP 工具仍只读，不执行 ERP 自动修复。既有 `lab/`、`deploy/` 和 `.runtime`
不会被迁移脚本接管或覆盖。企业运行目录、容器、网络、卷使用新的随机项目及双重标签。

| 服务 | 用途 | 对宿主开放 |
|---|---|---|
| db | PostgreSQL 17.11，工作台数据 | 正式模式无端口 |
| queue | Valkey 8.1.10，Celery broker | 正式模式无端口 |
| migrate | Alembic 一次性迁移，独立 DDL 账号 | 无 |
| api | FastAPI、Holmes、工作台认证 | 无 |
| worker | Celery prefork、并发 1，独立于 API | 无 |
| gateway | nginx 1.30.5，网页及 API | 127.0.0.1:8875 |
| prometheus | 3.13.4 LTS，`monitoring` profile | 无 |

`core` 是 internal 网络，数据库、队列和监控只加入此网络。API/worker 的独立 egress
桥用于明确授权的 HTTPS ERP/模型访问；默认 ERP allowlist 为空、模型不启用。
网关另有自己的 edge 网络。没有 Docker socket、host network、共享卷或生产环境配置。
nginx 不转发客户端伪造的 X-Forwarded-For，API 不信任所有代理 IP。
所有服务非 root、根文件系统只读，丢弃全部 capabilities，并设置 no-new-privileges、
健康检查、重启策略、日志轮转和资源上限。迁移服务为一次性任务，关闭持续健康探测；成功退出后才启动 API/worker。

## 当前本机演示

2026-10-10 已实际启动并保留工作台 `http://127.0.0.1:8875`，当前精确镜像为
`holmes-erp-enterprise:20261010-39edc000881c`。该实例只登记明确不可用的 `.invalid`
数据源及容器内延迟 fixture，用于测试任务队列和缺失证据；没有连接真实 ERP 或调用模型。
随机登录信息保存在当前项目 `state/initial-login.json`，请在本机私下查看，不复制到报告。

```powershell
Set-Location E:\ai\holmesgpt
$taskEnterprise = '.\examples\erpnext-support\enterprise\run.ps1'
$taskConfig = '.\examples\erpnext-support\enterprise\.runtime\holmes-enterprise-c63b69b8be5f\config-version-7fd288c93ab6.json'
& $taskEnterprise -Action Check -Config $taskConfig
# 需要暂停时仅停当前已验证项目，保留数据：
# & $taskEnterprise -Action Stop -Config $taskConfig
```

实际部署策略 52 项、保留数据更新 12 项、最终运行核验 19 项和新目标备份恢复 9 项
分别记录在 [VALIDATION.md](VALIDATION.md)，不把独立层级的检查点相加为业务场景数。
旧 `config.json` 对应旧镜像，保留用于追溯；操作当前实例须使用上面的版本配置。

## 新建与启动

从仓库根目录使用已有独立 Python 3.12 环境。构建上下文只复制源码、许可证和锁文件，
包括 TypeScript 的 `vite.config.ts`、`tsconfig.json`；不复制登录信息、数据库、旧报告或凭据。
主 `poetry.lock` 不重新生成，企业新增依赖单独固定在 `requirements.txt`。

```powershell
Set-Location E:\ai\holmesgpt
$taskEnterprise = '.\examples\erpnext-support\enterprise\run.ps1'
$taskBuild = (& $taskEnterprise -Action BuildContext).Trim()
$taskImage = 'holmes-erp-enterprise:' + (Get-Date -Format 'yyyyMMddHHmmss')
docker --context desktop-linux build --tag $taskImage $taskBuild
if ($LASTEXITCODE -ne 0) { throw '构建未成功，不能继续启动。' }
$taskConfig = ((& $taskEnterprise -Action Prepare -Image $taskImage) | ConvertFrom-Json).config
& $taskEnterprise -Action Start -Config $taskConfig -Monitoring
& $taskEnterprise -Action Check -Config $taskConfig
Invoke-RestMethod http://127.0.0.1:8875/ready
```

浏览器访问 `http://127.0.0.1:8875`，随机初始账号文件位于新项目的
`state/initial-login.json`。脚本不打印密码。客户 ERP 的 origin 必须由部署方明确授权并
写入该新目录 `state/settings.json` 的 `allowed_erp_origins`，API 凭据 JSON 放到
`state/credentials/`，服务端使用文件名别名绑定；不要放进前端、镜像或 Git。
如果要修改 settings/凭据，应在首次启动前完成；挂载到应用的这些目录/文件是只读的。

私有文件保存随机数据库密码、队列密码和 metrics token。数据库账号分开：
`postgres` 仅用于新数据库的初始化，不挂给 API；`workbench_migrator` 是非超级用户的
数据库/schema owner，仅迁移服务读取其 URL；`workbench_app` 无建库、建角色或 schema
CREATE 权限。迁移完成后授予应用必要 DML；audit_events 只授予 SELECT/INSERT。
Valkey 应用 ACL 禁 CONFIG、SHUTDOWN、DEBUG、FLUSHALL/FLUSHDB、ACL、MODULE、MIGRATE、
REPLICAOF/SLAVEOF，保留队列所需 EVAL/PUBSUB/PERSIST 等命令，禁用 Celery remote control。

应用连接从固定文件环境变量读取：`ERP_SUPPORT_DATABASE_URL_FILE`、
`ERP_SUPPORT_BROKER_URL_FILE`、`ERP_SUPPORT_METRICS_TOKEN_FILE`；迁移服务另有
`ERP_SUPPORT_MIGRATION_DATABASE_URL_FILE`。连接字符串不会出现在命令参数或设置 JSON 中。
模型需同时配置服务端 `model`/`model_api_base`、服务端环境 `ERP_SUPPORT_MODEL_API_KEY`、
对应环境 `model_consent=true`。当前验证没有调用真实模型。
Windows 生成目录只授予当前账号与 SYSTEM；Linux 部署应使新 `state` 的 owner 为
10001:10001，目录 mode 0700。selected secret 文件 mode 0444 位于 mode 0700 的私有父目录，
以便 PostgreSQL UID999 与应用 UID10001 读取各自明确挂载的文件。

## 检查、停止和备份

```powershell
& $taskEnterprise -Action Check -Config $taskConfig
& $taskEnterprise -Action Stop -Config $taskConfig
# Stop 保留数据；Down 只删除该已验证新项目的容器、网络和数据卷。
# 明确不再需要此项目数据时才调用：
# & $taskEnterprise -Action Down -Config $taskConfig
```

`/health` 只表示 API 存活；`/ready` 同时核对数据库、broker 和近期 worker heartbeat。
网关公开入口不提供 `/metrics`；Prometheus 仅在内部 core 网络连接未发布的网关 9091 端口，
该端口只代理 GET `/metrics`、固定内部 Host 并保留 Bearer 鉴权，其他路径拒绝。
token 来自私有文件，未授权请求拒绝，不把内部 api 主机名加入浏览器来源白名单。
首次 Holmes 依赖导入较慢，worker 健康检查有 120 秒启动宽限；默认并发 1 便于本机资源受限验证。
worker 检查只用 Python 标准库查询固定内部 API `/ready`，核验 DB、broker 及本项目近期 PG 心跳。
这适用于当前单 worker 部署；它不证明多个 worker 实例逐个健康。

```powershell
$taskBackup = ((& $taskEnterprise -Action Backup -Config $taskConfig) | ConvertFrom-Json).backup
```

备份使用 PostgreSQL `pg_dump` 的一致性快照，并输出新的 `.dump` 与 SHA256 清单。
它不包含 ERP 数据库本体，但包含敏感的工单证据、诊断结果、账号和会话状态。
数据库 URL、ERP/模型 Key、登录初始化文件和 metrics token 不在该备份内，应另行受控加密备份。
不要把备份作为工单附件或提交到 Git。

恢复只允许另一个全新项目的空数据库，不覆盖运行中的 schema/数据。先保留旧项目与镜像 digest：

```powershell
$taskRestoreConfig = ((& $taskEnterprise -Action Prepare -Image $taskImage) | ConvertFrom-Json).config
& $taskEnterprise -Action Start -Config $taskRestoreConfig -DataOnly
& $taskEnterprise -Action Restore -Config $taskRestoreConfig -Backup $taskBackup
# 独立恢复经授权的 settings/凭据后再启动全服务；migrate 会重新应用最小授权。
& $taskEnterprise -Action Start -Config $taskRestoreConfig
```

恢复会先撤销目标中的全部旧会话，账号及密码哈希以备份为准；新目录的随机初始化文件不会覆盖已有账号。
原 8875 项目应先 Stop，避免端口冲突。程序在任何创建操作之前解析并校验 Compose，
再对已有资源核对 ID、项目与随机标签、镜像、挂载、网络成员和端口；拒绝接管同名资源。

## 保留数据更新镜像

先用 `BuildContext` 生成另一个独占构建目录，并构建新的精确镜像标签，保留原镜像。
下面的 `Upgrade` 仅接受该模块已记录资源 ID 与标签的企业项目；需要当前 queued/running 任务为零。

```powershell
$taskNewBuild = (& $taskEnterprise -Action BuildContext).Trim()
$taskNewImage = 'holmes-erp-enterprise:' + (Get-Date -Format 'yyyyMMddHHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
docker --context desktop-linux build --tag $taskNewImage $taskNewBuild
if ($LASTEXITCODE -ne 0) { throw '新镜像构建失败，不执行更新。' }
$taskUpdated = (& $taskEnterprise -Action Upgrade -Config $taskConfig -Image $taskNewImage) | ConvertFrom-Json
$taskConfig = $taskUpdated.config
& $taskEnterprise -Action Check -Config $taskConfig
```

程序停止该项目 API 的新任务准入，再次检查数据库中的活跃任务，先生成一致性备份，
写新的 `config-version-<随机值>.json`，仅替换已验证的 migrate/API/worker 容器。
数据库、队列、原卷、登录文件和旧配置保留；新服务健康后重新加载本项目网关的 DNS。
所有后续检查/停止/备份应使用返回的新配置路径。

该原地更新已针对无 schema 变更的应用补丁验证。涉及 schema 变更时须先在新的空恢复目标
演练迁移与回退，不能假定新 migration 可逆。新镜像或启动失败时操作明确失败，
不会宣称自动回滚；保留数据库、备份、旧/新配置以便核验。
回退使用保留的精确旧镜像与全新恢复目标，先 Stop 原项目以让出 8875，
并按上节恢复受控 settings/凭据；不在有业务数据的数据库上覆盖或盲目 downgrade。

## 隔离测试及旧数据迁移

宿主 PostgreSQL/队列联调使用 `Prepare -Mode test` 的另一个新项目，只启动 db/queue。
Docker 29 的 internal 网络不发布宿主端口，因而 `compose.test.yaml` 显式将该测试项目的
core 改为桥网络，并把动态端口绑定到 127.0.0.1；此覆盖不用于正式企业模式。
测试连接文件仅保存在新项目的 `secrets/host_*_url`，不在终端显示。
测试数据与企业 API 数据使用不同数据库；权限和并发测试只能操作自己的测试库。

首版企业启动不会自动导入旧 SQLite，不修改旧 `.runtime`。
旧数据迁移的实际能力、原子提交/失败回滚及验证记录由持久层模块提供；迁移前需新建空
PostgreSQL 目标、备份原 SQLite、核对账号/环境授权/工单/证据/审计记录，成功后再切入口。
有新增 migration 时先备份，再运行受控 migrator；回滚使用旧精确镜像与另一个全新恢复目标，
不在已有数据库上盲目 downgrade 或覆盖旧配置。

`--unavailable-fixture` 只为本机真实队列故障测试创建 `.invalid` 保留域名与随机无效凭据；另允许
worker 容器内的 `http://127.0.0.1:19090` 延迟 GET fixture，以验证进程中断后的租约恢复。
它明确是不可用数据源，用于验证 partial/规则降级、任务留痕与重试，不能称为真实 ERP 联调。
版本依据、已验证步骤和实际失败记录见 [VERSIONS.md](VERSIONS.md) 与 [VALIDATION.md](VALIDATION.md)。
