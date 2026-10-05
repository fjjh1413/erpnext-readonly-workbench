# ERPNext v15 实验环境

需要本仓库已验证的独立 Python 3.12 `.venv` 与本机 Docker Desktop Linux 上下文
`desktop-linux`。这不是客户部署模板，不宣称支持其他 ERPNext 版本或生产安全审核。
宿主诊断程序运行于 Python 3.12；ERPNext 官方镜像自身使用它随镜像提供的 Python 3.11，
没有改造或冒称其为 Python 3.12 镜像。

```powershell
Set-Location E:\ai\holmesgpt
$taskPython = '.\.venv\Scripts\python.exe'
$taskERPConfig = (& $taskPython .\examples\erpnext-support\lab\lab.py create).Trim()
& $taskPython .\examples\erpnext-support\lab\lab.py up --config $taskERPConfig
& $taskPython .\examples\erpnext-support\lab\lab.py status --config $taskERPConfig
```

首次创建站点会实际安装 Frappe/ERPNext，并初始化独立业务数据。初始化失败会返回非零，
不得把缺失报告解释为成功。成功后返回仅绑定 127.0.0.1 的 ERP URL；
`connection.json` 保存接入地址和受控凭据文件引用，`secret.json` 仅保存只读 API key/secret，
不能复制到聊天、Git 或浏览器配置。管理员密码与数据库 root 密码也是 runtime 随机生成。

ERP 数据库、Redis、后台、队列与计划任务都只有项目的内部网络；只有 nginx 同时加入
该项目的入口网络，仅发布 `127.0.0.1:8090`。该端口已占用时启动失败，不接管已有服务。任何服务均不挂 Docker socket，不发布数据库或
Redis 端口。新随机项目为 `holmes-erp-<12位hex>`，不采用现有容器、卷或网络。

测试操作者可以在自己创建的配置范围内停止后台、数据库或缓存，并显式恢复：

```powershell
& $taskPython .\examples\erpnext-support\lab\lab.py fault --config $taskERPConfig --fault api_unavailable
& $taskPython .\examples\erpnext-support\lab\lab.py restore --config $taskERPConfig
# 其他允许的测试故障：database_unavailable / cache_unavailable

# 仅清理该配置创建并记录、且标签匹配的容器/网络/数据卷
& $taskPython .\examples\erpnext-support\lab\lab.py down --config $taskERPConfig
```

诊断工具没有这些写入与生命周期能力。关闭后需要新建配置，禁止接管原有资源。
`down --volumes` 删除的是该实验室自己的业务数据，正式客户备份/回退需按工作台运行文档执行。

实际 API 验证只读查证并测试账号应拒绝写入的方法，结果按新时间戳文件保存：

```powershell
& $taskPython .\examples\erpnext-support\lab\verify.py --config $taskERPConfig
```

可选的人工处理复核实验会在独立的 `LAB-RECHECK` 物料与新发货单上，由测试操作者
以实验 Administrator 身份执行补货 5、更新发货过账时点、提交发货 3，随后记录实际库存。
它还创建缺日志权限和无业务读取权限的 API 账号以及受限仓库发货单。
此命令只允许运行一次，保留原四个案例，不是诊断 Agent 的修复能力：

```powershell
& $taskPython .\examples\erpnext-support\lab\lab.py manual_recheck --config $taskERPConfig
```

输出 `recheck-result.json`，以及只在 runtime 受控目录保存的
`limited-secret.json` / `denied-secret.json`。不要打印或提交凭据。

模块单元测试不运行 Docker，也不连接 ERP：

```powershell
& $taskPython -m pytest -o addopts='' --confcutdir=examples\erpnext-support\lab\tests examples\erpnext-support\lab\tests -q
```

读账号只拥有自定义只读角色，配置测试公司与发货仓库 User Permission；不授予
Stock User 等带业务写权限的宽泛角色。原始错误日志可能包含业务敏感字段，工作台需在
进入证据、模型和前端之前脱敏并落实自己的环境授权。

只读扩展：`holmes_readonly.api.capabilities` 和
`holmes_readonly.api.stock_evidence(delivery_note,line_name)`，均要求认证且仅允许 GET。
未适配的批次/序列号、库存维度、自定义工作流或库存重算状态进入明确 limitations，
诊断账号读取权限不能证明故障操作用户拥有提交权限。
测试主账号明确授予 System Settings 只读权限以核验站点时区；缺少此权限时返回空时区与
`site_timezone_unverified`。本版实际验证 Company/Warehouse 的 User Permission 范围；
其他 User Permission 维度、定制 Stock Ledger Entry
权限钩子会拒绝库存聚合。运行层 DB/Redis 探测未由该 ERP 扩展实现，明确返回
`runtime_health_not_available`，不以普通 ERP API 可达推断数据库和缓存健康。

版本与源码依据见 [VERSIONS.md](VERSIONS.md)。实际集成结果由验证脚本生成，mock 单元测试
与真实 ERP 请求分别记录；未调用模型的实验不得称为模型准确率评测。
