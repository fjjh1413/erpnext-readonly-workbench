# 企业存储与安全迁移

工作台独立使用 PostgreSQL；ERP 数据库与 API 凭据不存入这些表。SQLite 保留为本机测试模式。
SQLAlchemy 2 使用主锁中的现有依赖，Alembic 1.18.4 由企业独立 requirements 固定，不修改上游锁文件。

`records` 保存兼容旧接口的 JSON，独立 `version` 列配合事务行锁和条件更新防止丢失修改。
`sessions` 保存 Token 哈希；禁用、角色/授权或密码变化撤销登录。`audit_events` 是独立审计，
应用账号仅能读取和追加。`job_leases` 保存持久队列及 Worker 租约，不依赖内存任务列表。
`usernames` 限制大小写无关的重复账号，账号变更事务保护最后一个可用管理员。

创建任务同时保存当前工单快照并改变工单状态；全局调度锁和工单行锁防止重复排队及突破容量。
重复 Celery 消息不能再次认领；进度、心跳、完成发布均校验当前租约所有者。PostgreSQL 使用
数据库钟点计算租约。过期任务保留既有步骤并标记中断，不停止其他 Worker 或待发队列。
人工动作、最新复核关闭与环境删除也使用数据库事务；分页先应用完整作用域，再计数和截取。

首次部署以迁移账号执行 `initialize_database` 和 `grant_application`，应用账号不具备
数据库/Schema CREATE 或审计 UPDATE/DELETE 权限。API 启动仅核验 Schema，不自动运行 DDL。
后续 Alembic 迁移由同一独立迁移路径执行；禁止破坏性 downgrade，恢复应使用另一全新数据库。

## 旧 SQLite 复制迁移

先停止旧本机写入服务、备份源文件，准备全新数据库以及私有迁移连接文件。文件内容是完整
`postgresql+pg8000://...` 连接串，含凭据，必须用文件权限控制；不能放进工具参数、聊天或 Git。
迁移命令仅输出数量和结果，不输出连接串、记录、密码哈希或密钥。

```powershell
# 独立 Python 3.12 环境；路径按本机受控文件替换
& E:\ai\holmesgpt\.venv\Scripts\python.exe -m holmes.erpnext_support.persistence.migrate copy-legacy `
  --source E:\受控目录\原工作台.sqlite3 `
  --database-url-file E:\受控目录\迁移连接.txt

# 核对只读 dry-run 的数量，再对同一全新空目标实际导入
& E:\ai\holmesgpt\.venv\Scripts\python.exe -m holmes.erpnext_support.persistence.migrate copy-legacy `
  --source E:\受控目录\原工作台.sqlite3 `
  --database-url-file E:\受控目录\迁移连接.txt --apply
```

目标已有记录、来源与目标相同、来源有链接/联接、格式不合法或超出已审查批次上限时拒绝。
源以 SQLite `mode=ro` 打开；目标所有数据在一个事务中导入，中途失败不留下部分记录。
登录会话不迁移，用户需要重新登录。旧排队/运行任务标记中断并保留证据，不自动重放。
旧账号密码哈希、工单、处理记录、案例及审计保留；API 密钥文件需单独受控复制。
自动导入范围限制为源文件512MiB、10万记录、单条8MiB，超过范围应另行审查批次方案。

## 已实际验证

2026-10-10：SQLite及独立 PostgreSQL 的存储/原核心/迁移/权限组合回归为64项通过、
一次性迁移案例1项未重复执行，耗时97.62秒。真实 PostgreSQL 不使用数据库 mock。
独立 `enterprise_migration_test` 的实际 SQLite→PostgreSQL 复制与权限组合为12项通过、
49.01秒；源SHA256保持不变，应用账号无法建表或修改、删除审计事件。
这两轮有重复场景，不累加为76种业务场景。JUnit 位于项目 reports 目录。
末尾主动撤回不应授予的权限、并发备份独占保留和关闭状态检查的定向回归为14项通过、54.86秒；
SQL端限制每类200条的原子快照及当前工单快照、备份和关闭复验为10项通过、73.62秒。
追加断开 Store 后重新打开的持久 outbox 定向复验为2项通过、53.67秒，分别使用 SQLite
和真实独立 PostgreSQL。报告为 `enterprise-outbox-reopen-20261010.xml`。
后续真实 UI/PG 联调发现并修正审计自由搜索漏项和 pg8000 JSON 分组参数问题：审计搜索
覆盖已脱敏资源编号、类型与嵌套详情，支持中文 JSON 和 `%`/`_` 字面匹配；指标按固定
`task_state` 别名分组，避免 SELECT/GROUP BY 的重复 JSON 表达式生成不同位置参数。
两处定向回归 SQLite 7项通过、42.91秒，真实新建独立 PostgreSQL 7项通过、45.49秒。
报告分别为 `enterprise-audit-metrics-sqlite-20261010.xml` 和
`enterprise-audit-metrics-pg-20261010.xml`；后者执行真实 pg8000 查询，没有数据库 mock。
单纯 SQL 方言编译不足以确认驱动实际执行，以上结果取自实际数据库执行。
这些定向复验同样不重复累计场景数。

并发测试覆盖36次更新无丢失、同工单八个并发提交只创建一个任务、最后管理员并发保护、
重复/错误/过期 Worker 拒绝发布、重新打开队列、分页作用域以及超过前200条后的环境引用检查。
这些是功能和并发合同测试，未声明生产规模压测、跨机故障模型或任意数据库兼容性。

新增 `.github/workflows/erp-support-enterprise.yml` 在相关路径变更时检查 Python 3.12
SQLite/API/Agent 契约、Alembic 与迁移 dry-run，以及 Node 24 前端类型、构建和全部 mock 组件。
流程仅上传三个固定路径的 JUnit 文件，不收集运行目录、连接文件或业务记录。
该流程已在本机检查 YAML 和测试选择；没有声明 GitHub 已执行，更不将 mock 当作真实 ERP、
Docker 或 PostgreSQL 联调结果。真实 PostgreSQL 结果仅来自上述独立本机实验。

实现依据：[SQLAlchemy 2事务](https://docs.sqlalchemy.org/en/20/core/connections.html#using-transactions)、
[PostgreSQL 行锁](https://www.postgresql.org/docs/current/explicit-locking.html)、
[Alembic迁移](https://alembic.sqlalchemy.org/en/latest/tutorial.html)。
