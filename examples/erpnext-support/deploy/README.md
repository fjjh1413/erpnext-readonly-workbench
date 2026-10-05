# 单客户工作台 Docker 交付

工作台镜像包含后端和已构建的 Vue 页面。ERPNext、MariaDB、Redis 和 Docker daemon
均不进入本 Compose；没有 Docker socket 挂载。宿主 Python 使用独立 3.12 环境，
构建固定 Node 24.21.0、Python 3.12.10 与 Poetry 1.8.5，复用主 `poetry.lock`
和前端 `package-lock.json`，不重新生成主锁文件。

该模板仅监听 `127.0.0.1:8765`。外部用户访问需要部署方提供 HTTPS 反向代理、
明确配置工作台 origin 并核验认证/日志/备份规则。ERP 的非本机目标只允许明确列入
服务端配置的 HTTPS origin；凭据按服务端别名存放在私有 runtime，浏览器与模型不选择凭据文件。
没有默认客户 ERP 地址、模型地址或密钥。

启用模型需同时满足三项：私有 `settings.json` 明确配置 `model` 与 `model_api_base`，
服务端进程环境设置 `ERP_SUPPORT_MODEL_API_KEY`，对应工作台环境明确设置 `model_consent=true`。
Compose 仅将该可空的服务端环境变量传入容器；不要将 Key 放入镜像、前端、报告或 Git，
不要输出包含密钥的 Compose 配置。外部模型目标必须使用 HTTPS，模型将收到必要且脱敏的业务证据。
本次部署验证没有配置或调用真实模型。

从仓库根目录构建，先生成仅含源文件和锁文件的新上下文；不会复制 `.runtime`、
已有手工报告、Git 历史、用户配置、虚拟环境或登录/ERP 凭据：

```powershell
Set-Location E:\ai\holmesgpt
$taskPython = '.\.venv\Scripts\python.exe'
$taskBuildContext = (& $taskPython .\examples\erpnext-support\deploy\prepare.py build-context).Trim()
$taskImage = 'holmes-erp-workbench:' + (Get-Date -Format 'yyyyMMddHHmmss')
docker build --tag $taskImage $taskBuildContext
if ($LASTEXITCODE -ne 0) { throw '镜像没有构建成功，请查看实际错误。' }
```

准备新客户运行目录，必须输入已经授权的真实 ERP HTTPS origin；若仅验证登录和页面，
可以省略 `--erp-origin`，这时 ERP allowlist 为空，诊断连接不可用：

```powershell
$taskERPOrigin = Read-Host '输入已授权 ERP HTTPS origin，例格式为 https://域名:端口'
$taskRuntime = (& $taskPython .\examples\erpnext-support\deploy\prepare.py runtime --erp-origin $taskERPOrigin).Trim()
if ($LASTEXITCODE -ne 0) { throw '未创建客户运行目录。' }
# 在该新目录的 credentials\ 中放置单客户的只读 API 凭据 JSON，文件名即服务端别名。
# 不将凭据打印到终端，不复制到前端，也不提交到 Git。
$taskInstanceId = [Guid]::NewGuid().ToString('N')
$taskProject = 'holmes-workbench-' + $taskInstanceId.Substring(0,12)
if (docker ps -aq --filter "label=com.docker.compose.project=$taskProject") { throw '项目已存在，拒绝接管。' }
if (docker network ls -q --filter "label=com.docker.compose.project=$taskProject") { throw '项目网络已存在，拒绝接管。' }
$env:WORKBENCH_IMAGE = $taskImage
$env:WORKBENCH_RUNTIME_DIR = $taskRuntime
$env:WORKBENCH_INSTANCE_ID = $taskInstanceId
docker compose --project-name $taskProject --file .\examples\erpnext-support\deploy\compose.yaml up --detach
```

以该目录 `initial-login.json` 中的随机登录信息访问 `http://127.0.0.1:8765`；
这与 ERP API 凭据不同。登录文件/SQLite/ERP 凭据留在同一受限目录，由部署方设置
Windows ACL 或 Linux 文件所有者为运行账号 `10001:10001`。容器根文件系统只读，
只有私有 `/state` 与临时 `/tmp` 可写；启动不执行 ERP 自动修复。

查看和停止：

```powershell
docker compose --project-name $taskProject --file .\examples\erpnext-support\deploy\compose.yaml ps
Invoke-RestMethod http://127.0.0.1:8765/health
# 只停止该新项目，保留 runtime 的工作台数据与凭据
docker compose --project-name $taskProject --file .\examples\erpnext-support\deploy\compose.yaml stop
```

数据库备份调用工作台自己的 SQLite backup API，输出必须是全新文件：

```powershell
$taskBackup = Join-Path $taskRuntime ('backup-' + (Get-Date -Format 'yyyyMMddHHmmss') + '.sqlite3')
& $taskPython -m holmes.erpnext_support backup --settings (Join-Path $taskRuntime 'settings.json') --output $taskBackup
```

该备份不包括 ERP 数据库、API 凭据、登录初始化文件或模型密钥。凭据和明确配置另行加密备份，
备份目录限制为管理员可读。不要将它们加入工单附件或镜像。

升级前保留旧镜像的实际 digest、SQLite 新备份与独立受保护的配置/凭据副本。回滚先停止
此工作台项目，使用上一版本精确镜像标签/digest；需恢复数据时，先将备份完整性检查并恢复到
全新的文件/运行目录，禁止覆盖运行中的 SQLite，再切换私有挂载。原运行目录保留供核对。
恢复命令示例：

```powershell
# $taskRestoreFile 必须是操作者明确选择的新目录中的不存在文件。
& $taskPython -m holmes.erpnext_support restore --backup $taskBackup --output $taskRestoreFile
```

镜像健康检查和登录成功只证明工作台启动；它们不证明客户 ERP、数据库、Redis 或真实模型
已验证。容器访问宿主机演示 ERP 需要另行受审查的 transport，本交付不绕过 HTTPS
或把 `host.docker.internal` 当作未经核验的 ERP 目标。

[Node 官方版本依据](https://github.com/nodejs/nodejs.org/blob/main/apps/site/pages/en/blog/release/v24.21.0.md)。
实际构建与容器检查记录见本目录运行验证报告，未完成步骤明确记录为未完成。
