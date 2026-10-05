# Docker Web 系统只读故障诊断（中文运行文档）

本次新增首版面向隔离的 **FastAPI + MySQL + Redis** 测试应用。
代码已集成进 HolmesGPT 的 `Tool/Toolset` 与 `ToolExecutor`；可选模型分析复用原 `ToolCallingLLM`。
默认模式无需模型密钥，执行固定采证与证据规则，不声称是已完成的真实模型评测。

上游完整仓库位于 `E:\ai\holmesgpt`，开发分支为 `feat/docker-readonly-diagnosis`。
原有许可证 `LICENSE` 和通用功能保留。上游基准、源码审查及新增模块归属见
[DEVELOPMENT.md](DEVELOPMENT.md)。

## 环境与快速运行

需要 Windows 上的 Python **3.12**、Git、Docker Desktop Linux 引擎与 Docker Compose。
使用项目内 `.venv`，不修改全局 Python；已有目录、环境或报告不会被删除或直接替换。
本次验证的依赖版本保存在 [requirements-python312-windows.txt](requirements-python312-windows.txt)，
上游 `poetry.lock` 保持原样。默认 bootstrap 使用实际验证环境快照；
若需上游锁文件环境可显式加 `-Dependencies UpstreamLock`，该选项使用 Poetry 1.8.5，不重新生成锁文件。

```powershell
Set-Location E:\ai\holmesgpt
git status --short
py -3.12 --version
docker version
docker compose version

# 使用已存在且已验证的独立 .venv，或首次创建；不覆盖其他环境
.\examples\docker-readonly\bootstrap.ps1
$taskPython = '.\.venv\Scripts\python.exe'
& $taskPython -c 'import sys; print(sys.version); print(sys.prefix != sys.base_prefix)'

# 自动创建全新实验室，验证正常场景和三类故障，最终仅清理自身容器/网络/数据卷
.\examples\docker-readonly\run.ps1 -Action evaluate
```

`run.ps1` 为每次评测生成新的 `reports/run-...` 目录。也可指定尚不存在的目录：

```powershell
.\examples\docker-readonly\run.ps1 -Action evaluate -Output .\examples\docker-readonly\reports\my-first-run
```

若当前 PowerShell 策略禁止启动本地脚本，可在单独进程中运行以下命令（不改系统执行策略）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\examples\docker-readonly\bootstrap.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\examples\docker-readonly\run.ps1 -Action evaluate
```

初次运行会拉取固定版本镜像并构建测试 API；离线、镜像拉取或 Docker 权限失败会明确报错。
不使用 `latest`，不依赖生产数据库、缓存或任何真实业务凭据。

## 手动故障复现

每个实验室有随机项目名 `holmes-lab-<12位hex>` 和独立运行标签。运行时文件位于
`.runtime/<project>/`；凭据随机生成，运行时目录限制本机访问且被 Git 忽略。
生命周期脚本拒绝接管已存在的 Docker 资源。

```powershell
Set-Location E:\ai\holmesgpt
$taskPython = '.\.venv\Scripts\python.exe'
$taskLabConfig = (& $taskPython .\examples\docker-readonly\lab.py create).Trim()
& $taskPython .\examples\docker-readonly\lab.py up --config $taskLabConfig
& $taskPython .\examples\docker-readonly\lab.py status --config $taskLabConfig

# 正常采证；每次指定新的报告文件，已有报告会被拒绝覆盖
& $taskPython -m holmes.core.docker_diagnosis --config $taskLabConfig `
  --output .\examples\docker-readonly\reports\manual-healthy.json

# 故障一：仅停止该实验室的 MySQL；真实 /ready 返回依赖失败
& $taskPython .\examples\docker-readonly\lab.py fault --config $taskLabConfig --fault database_unavailable
& $taskPython -m holmes.core.docker_diagnosis --config $taskLabConfig `
  --output .\examples\docker-readonly\reports\manual-database.json
& $taskPython .\examples\docker-readonly\lab.py restore --config $taskLabConfig

# 故障二：API Redis 端口改成 6380，Redis 本身仍正常
& $taskPython .\examples\docker-readonly\lab.py fault --config $taskLabConfig --fault cache_connection_error
& $taskPython -m holmes.core.docker_diagnosis --config $taskLabConfig `
  --output .\examples\docker-readonly\reports\manual-cache.json
& $taskPython .\examples\docker-readonly\lab.py restore --config $taskLabConfig

# 故障三：APP_MODE=invalid，真实 FastAPI 启动配置校验失败并退出
& $taskPython .\examples\docker-readonly\lab.py fault --config $taskLabConfig --fault service_configuration_error
& $taskPython -m holmes.core.docker_diagnosis --config $taskLabConfig `
  --output .\examples\docker-readonly\reports\manual-config.json
& $taskPython .\examples\docker-readonly\lab.py restore --config $taskLabConfig

# 测试操作者主动清理；这不是诊断 Agent 自动修复
& $taskPython .\examples\docker-readonly\lab.py down --config $taskLabConfig
```

恢复只由测试脚本显式执行，恢复后重新验证实际 `/ready` 成功；诊断入口没有恢复方法或写操作工具。
已关闭的项目需要重新 `create`，不能自动采用别的项目。容器创建失败时，同一配置仍可执行 `down`，
只清理已记录且标签匹配的测试资源；不会执行全局 prune 或清理其他容器。

## 工具与报告约定

| 工具 | 输入与实际检查 |
| --- | --- |
| `docker_lab_logs` | 逻辑服务 api/mysql/redis；tail 1..200、窗口 1..3600 秒、可选最长 100 字符的字面过滤；合并输出最多 64 KiB，返回截断标记 |
| `docker_lab_health` | 逻辑服务 api/mysql/redis；只返回状态、健康、退出码和 OOM 摘要 |
| `docker_lab_config` | 无参数；仅返回 API 的 MYSQL_HOST/PORT、REDIS_HOST/PORT、APP_MODE |
| `docker_lab_mysql` | 无参数；固定探针执行固定 `SELECT 1`，账号只具 USAGE |
| `docker_lab_redis` | 无参数；固定探针执行 `AUTH probe` 和 `PING`，ACL 禁止数据读写与配置命令 |

所有网络都是本项目的内部网络，服务没有对宿主/局域网发布端口。
增加的 probe 容器使用非 root、只读文件系统、移除 capabilities，不挂 Docker socket。
工具校验本地 Docker 上下文、项目/运行 ID/服务标签和全部网络成员。
连接探测只启动 probe 的固定程序、固定动作，不接受任意主机、端口、SQL、容器 ID 或命令。
固定进程调用也计入工具审计记录。Docker 读取有 8 秒超时，探测有短超时。

日志与模型文本均视作不可信数据。运行时凭据、常见密码/token、URL 用户信息与 Authorization 格式
在返回前脱敏；未验证参数不会写入工具描述或记录。通用上游 Docker/Bash 工具不在此入口中启用。

报告包含 `confirmed_facts`、`candidate_causes`、`supporting_evidence`、`missing_information`、
`next_steps`、`tool_calls` 和 `metrics`。每个事实/候选引用真实成功调用的证据 ID。
证据不足、无日志、截断、权限拒绝、超时都会明确保留，不能将推测写成已确认根因。

成功率 = `status=success` 的工具调用数 / 全部工具调用数。
成功探测发现 `reachable=false` 表示故障被观测到，仍是调用成功；工具未完成则记为失败。
采证耗时不含导入启动、拉取镜像、构建应用与故障注入；评测 summary 另外记录完整实验耗时。
探针正常不等于应用配置路径或业务请求正常，报告始终说明这一边界。

## 自动化验证与真实记录

```powershell
Set-Location E:\ai\holmesgpt
$taskPython = '.\.venv\Scripts\python.exe'

# 单元、安全边界、上游 Agent 接线测试；真实 Docker 用例默认跳过
& $taskPython -m pytest -c tests/docker_readonly/pytest.ini --confcutdir=tests/docker_readonly tests/docker_readonly -q
& $taskPython -m pytest -c tests/docker_readonly/pytest.ini --confcutdir=examples/docker-readonly/tests examples/docker-readonly/tests -q

# 显式开启真实 Compose 实验，用新目录保留 JSON（串行运行）
$env:HOLMES_READONLY_LIVE = '1'
$env:HOLMES_READONLY_REPORT_DIR = 'E:\ai\holmesgpt\examples\docker-readonly\reports\my-pytest-run'
$env:HOLMES_READONLY_RESTORE_REPORT = 'E:\ai\holmesgpt\examples\docker-readonly\reports\my-permissions-restore.json'
& $taskPython -m pytest -c tests/docker_readonly/pytest.ini --confcutdir=tests/docker_readonly tests/docker_readonly/test_live_faults.py -s -q
& $taskPython -m pytest -c tests/docker_readonly/pytest.ini --confcutdir=tests/docker_readonly tests/docker_readonly/test_live_permissions_restore.py -s -q
Remove-Item Env:HOLMES_READONLY_LIVE
Remove-Item Env:HOLMES_READONLY_REPORT_DIR
Remove-Item Env:HOLMES_READONLY_RESTORE_REPORT
```

实际执行结果与首次部署问题记录见 [VALIDATION.md](VALIDATION.md)，报告位于 `reports/`。
scripted LLM 测试仅验证原 Agent 循环接线、工具白名单、记录与证据过滤，没有访问真实模型服务。
全上游测试、生产行为和长期可靠性不由少量隔离实验证明。

## 可选模型分析

默认不访问模型。只有显式添加 `--model` 才执行上游模型调用循环（最多 4 轮），
且仍只有这五个工具。模型所需凭据由调用者设置环境变量，不能写入源码或报告：

```powershell
# 使用你已经配置好的 Holmes/LiteLLM 模型；替换占位模型名
& .\.venv\Scripts\python.exe -m holmes.core.docker_diagnosis --config $taskLabConfig `
  --output .\examples\docker-readonly\reports\manual-model.json --model '<已配置的模型名>'
```

需要统一传入模型 API key 时，入口读取 `HOLMES_DIAGNOSIS_MODEL_API_KEY`，也支持上游提供方环境配置。
模型只接收脱敏后的测试证据；确认允许发送这些证据到所选提供方后再显式使用。
模型输出单列于 `model_hypotheses`，不能成为已确认事实；伪造或失败证据引用被剔除。
模型失败时仍保留固定采证报告，并记录缺失信息。
若模型在后续轮次失败，此前已完成的模型工具调用也继续保存在审计记录中。
