# Docker 只读测试诊断（本次新增）

这是基于 HolmesGPT 的本地二次开发，首版只接受本次生成的隔离 FastAPI + MySQL + Redis 测试项目。
上游 Docker 工具及通用 Agent 均保留；专用入口显式只加载新增的 `docker/readonly-lab` 工具集。

完整中文运行步骤、隔离边界、故障复现与真实验证记录见仓库
[examples/docker-readonly/README.md](../../../examples/docker-readonly/README.md)。

```powershell
# 在仓库根目录运行；config.json 由测试实验室生成
.\.venv\Scripts\python.exe -m holmes.core.docker_diagnosis `
  --config examples/docker-readonly/.runtime/<project>/config.json `
  --output examples/docker-readonly/reports/<new-report>.json
```

默认使用固定只读采证和规则解释，不访问模型服务。报告的 `confirmed_facts` 来自成功的工具观测，
`candidate_causes` 必须有实际证据引用。缺失或失败的观测进入 `missing_information`。
只有显式传入 `--model` 才调用原 `ToolCallingLLM`；模型候选单列于 `model_hypotheses`，不进入已确认事实。

所有工具调用保留原 `ToolCallResult` 和 `StructuredToolResult` 字段。数据库或缓存探测发现不可达仍为
成功的检查观测；工具范围拒绝、权限错误和执行超时才降低 `tool_success_rate`。

工具只接受固定逻辑服务，不接受任意容器、主机、端口、SQL 或命令。日志有行数、时间、字节上限，
环境配置只返回非敏感字段。凭据从本次运行时 secret 文件读取，记录与报告经过脱敏。
