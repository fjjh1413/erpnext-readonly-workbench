# 改造审查与功能归属

上游：[HolmesGPT/holmesgpt](https://github.com/HolmesGPT/holmesgpt)，完整克隆基准提交
`9e21560c7ad1d02fdac29c9d9979999992bc3edb`。原 `LICENSE`（Apache-2.0）保留且不修改。
本文与同目录测试实验室、`holmes/core/docker_diagnosis.py`、
`holmes/plugins/toolsets/docker_readonly/`、`tests/docker_readonly/` 为本次新增工作。

## 上游源码审查

原 CLI `holmes/main.py` 经 `holmes/config.py` 组装 `ToolCallingLLM`。
`holmes/core/tool_calling_llm.py` 中的循环执行模型工具请求，
`holmes/core/tools_utils/tool_executor.py` 管理已启用工具，
`holmes/core/tools.py` 定义 `Tool`、`Toolset`、参数 schema、审批、执行和结构化结果。
Python 扩展在 `holmes/plugins/toolsets/__init__.py` 注册。
`holmes/core/models.py` 的 `ToolCallResult` 已支持调用 ID、工具名与结构化观测。

原 `holmes/plugins/toolsets/docker.yaml` 是通用 Docker shell 工具集，能查看整个 Docker daemon；
详情包含环境配置，日志无界，事件监听可阻塞。直接复用它不能满足本次访问范围和脱敏要求。
原连通性检查支持主机允许列表，但首版暴露主机和端口仍过宽；
原数据库连接器在 prerequisites 探测在线性，数据库停机时可能禁用整个工具。

原测试位于 `tests/`，模型评测位于 `tests/llm/`；全局 conftest 载入多种远程评测集成，
默认测试参数也启用并行与全仓覆盖率。新增本地实验测试使用独立 pytest 配置及 confcutdir，
避免载入无关的外部服务配置，真实故障测试串行执行。

## 最小改造方案与实施

1. 继承原 Python `Tool/Toolset`，新增严格输入校验、项目和运行 ID 标签检查、本地 Docker 上下文检查、
   有界日志、健康摘要、固定 MySQL SELECT 1、Redis AUTH/PING、非敏感配置摘要。
2. 专用入口直接构造只含新工具集的 `ToolExecutor`；不载入用户主目录配置，不自动发现其他工具。
   固定采证也执行原 `Tool.invoke` 并保存 `ToolCallResult`。显式模型模式复用原 `ToolCallingLLM`。
3. 诊断结构验证每个事实/候选引用的证据确实来自成功调用。模型只提出未经确认的候选。
4. 独立 Compose 内部网络、临时凭据、只读数据库/缓存账号及固定只读探针构成可重复实验室。
   故障注入和恢复属于测试 harness，诊断工具集本身不提供生命周期或写入操作。
5. 单元测试验证边界和证据规则；scripted LLM 测试验证上游循环接线；真实 Compose 故障评测记录
   实际诊断 JSON、调用成功率和采证耗时。scripted LLM 不视为真实模型评测。

原架构不重写，原功能保留。新增文件的模块说明明确标识 downstream/addition。
自动修复、生产连接、任意 SQL、通用 shell、外部通知不在专用诊断入口中。

## 实际部署验证引起的调整

首次真实启动时，API 在容器内部健康，但宿主端口没有被发布，宿主 `/ready` 验证失败。
实际 inspect 显示 HostConfig 中存有请求映射，NetworkSettings.Ports 却为空。
这是[仅 internal 网络无法发布端口的上游行为](https://github.com/moby/moby/discussions/53256)。
因此保留全内部网络，改为非 root、只读文件系统、无 Docker socket 的固定探针服务。
工具只允许启动该探针的固定程序及固定 mysql/redis 操作；不提供任意 Docker exec 或 shell 接口。
连接目标仍是网络内的固定 mysql:3306、redis:6379，凭据仍来自本次运行时 secrets。
