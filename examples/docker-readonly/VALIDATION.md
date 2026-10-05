# 实际验证记录

验证日期：2026-10-05（Asia/Shanghai）。实际宿主为 Windows 10，独立 `.venv` Python 3.12.10，
Docker Engine 29.8.1 / Compose 5.5.1。上游克隆基准为
`9e21560c7ad1d02fdac29c9d9979999992bc3edb`，上游 LICENSE、pyproject.toml、poetry.lock 未修改。
依赖安装发生于 `.venv`，`pip check` 实际通过。

## 真实 Compose 故障评测

以下为一次真实运行的结果，逐场景创建新项目，诊断只收到范围配置，不知道注入故障名称。
此轮模式是 `deterministic-evidence`，没有调用真实 LLM。

| 场景 | 实际候选/观测 | 工具调用 | 诊断耗时 | 验证 |
| --- | --- | --- | --- | --- |
| 正常 | 无故障候选，明确不能据此证明全部业务正常 | 9/9 成功 | 25.129 秒 | 通过 |
| 数据库不可用 | MySQL 已停止，固定探测不可达，候选 database_unavailable | 9/9 成功 | 17.166 秒 | 通过 |
| 缓存连接错误 | 独立 Redis PING 正常，API REDIS_PORT=6380，候选 cache_connection_error | 9/9 成功 | 21.410 秒 | 通过 |
| 服务配置错误 | API APP_MODE=invalid、启动退出与真实错误日志，候选 service_configuration_error | 9/9 成功 | 21.521 秒 | 通过 |

共 **36/36** 次工具调用成功（100%）。各场景仅执行一次，不能推断长期成功率或真实模型准确率。
采证耗时包含范围验证及 Docker 工具调用，不含导入、镜像下载、构建、故障注入与清理。
评测 harness 完整耗时 482.134 秒；pytest 含导入和收集共 517.13 秒，1 项真实集成测试通过。

原始记录：

- [summary.json](reports/actual-20261005-01/summary.json)
- [正常诊断](reports/actual-20261005-01/01-healthy.json)
- [数据库故障诊断](reports/actual-20261005-01/01-database_unavailable.json)
- [缓存配置故障诊断](reports/actual-20261005-01/01-cache_connection_error.json)
- [服务配置故障诊断](reports/actual-20261005-01/01-service_configuration_error.json)
- [真实集成 pytest 记录](reports/live-20261005-01.xml)

报告包含已确认事实、候选原因、实际证据、缺失信息、建议及原 ToolCallResult 审计记录。
数据库不可达属于成功观测到故障，并非一次工具执行失败。

## 实际权限与恢复验证

另建独立测试项目，实际尝试的 MySQL CREATE DATABASE 被拒绝（错误码 **1044**），
Redis SET 被 ACL 拒绝。这些拒绝写入测试只属于测试 harness，未注册为诊断 Agent 工具。
随后依次注入三类故障并显式恢复，每次真实 `/ready` 返回 200 且 MySQL/Redis 均为 ok。
此项测试实际 **1 passed，183.83 秒**。

[权限与三次恢复观测](reports/permissions-restore-20261005.json)，
[pytest 记录](reports/permissions-restore-20261005.xml)。所有本次实验容器、网络和数据卷已清理；
原先存在的三个 nginx 容器保持运行。

## 单元与上游回归

- 新增工具边界、诊断解释及原 Agent 接线最终重测：80 项通过，2 项真实 Compose 用例在普通单元运行时显式跳过，151.68 秒。
  [最终记录](reports/unit-delivery-20261005.xml)。上述两项真实集成测试已分别显式开启并实际通过。
- 新增实验室生命周期与固定 probe：70 项通过（37 + 33），[记录](reports/lab-probe-unit-20261005.xml)。
- 上游 `test_tool_calling_llm.py` 与 `test_json_schema_coerce.py`：80 项通过，
  [记录](reports/upstream-regression-20261005.xml)。
- 可选模型接线使用 scripted mock，验证真实上游工具执行、越权拒绝、虚构证据过滤和脱敏；
  这些结果不属于真实模型评测。最终回归还验证模型后续失败时保留此前全部已完成工具记录。
- PowerShell 包装脚本语法检查及对已关闭测试项目的 `run.ps1 -Action status` 实际通过。

新增诊断模块最终定向单元总覆盖率为 **86.60%**（启用分支统计；语句 88.15%、分支 81.73%），
[最终 JSON](reports/coverage-delivery-20261005.json)。该值只针对新增诊断模块，不代表全上游覆盖率。
首次成功测量的总覆盖率为 85.99%（分支 81.31%），原始
[JSON](reports/coverage-final-20261005.json)保留。最终重测包含模型部分记录保护改动。

## 失败与修正记录

第一次真实部署时，API 在容器内部健康，但纯 internal 网络不能发布宿主端口，宿主验证失败。
未将该次运行计为通过。其测试资源已清理；改为全内部网络的固定只读 probe 后，上述真实评测通过。

首次覆盖率命令在收集时遇到第三方 Pydantic/LiteLLM `KeyError: pydantic.root_model`，
[失败记录](reports/unit-20261005.xml)予以保留。先显式导入该模块、再启动 pytest 覆盖率测量后通过。
成功重测命令形如：

```powershell
.\.venv\Scripts\python.exe -c "import pydantic.root_model; import pytest; raise SystemExit(pytest.main(['-c','tests/docker_readonly/pytest.ini','--confcutdir=tests/docker_readonly','tests/docker_readonly','-q','--cov=holmes.plugins.toolsets.docker_readonly','--cov=holmes.core.docker_diagnosis','--cov-fail-under=40']))"
```

未运行全部上游测试、真实模型服务或任何生产验证。上游 Typer/Click 弃用警告保留，未修改无关依赖。
真实 Compose 评测时的源码 SHA256 快照位于
[原始快照](reports/source-manifest-20261005.json)，最终运行代码与测试快照位于
[交付快照](reports/source-manifest-delivery-20261005.json)。之后的模型失败记录保护改动由最终 mock 单测验证，
未重跑真实模型服务。原始实际报告不覆写。凭据扫描只报告匹配数量，不输出凭据内容。
[最终交付核对](reports/delivery-checks-20261005.json)记录依赖一致性、原文件保护、源码快照、
报告及暂存内容凭据扫描、实验资源清理和原有容器状态。新增 `.gitattributes` 保证容器 shell 脚本在 Windows 重新检出后仍为 LF。
