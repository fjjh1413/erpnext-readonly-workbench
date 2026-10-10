# 实际验证记录

2026-10-10 本机已执行：

- 四个基础组件精确版本镜像真实拉取成功，digest 已写入 Compose/VERSIONS。
- 第一临时 test 项目 PostgreSQL 成功，但 Valkey 8 系 CLI 未识别仅设置的 VALKEYCLI_AUTH，
  健康等待实际失败。已改为同时设置兼容的 REDISCLI_AUTH/VALKEYCLI_AUTH；只清理该项目的
  已记录容器/网络/卷，保留其私有失败运行目录，没有接管其他环境。
- 新 test 项目 `holmes-enterprise-b926a562bad5` 的 db/queue 均真实 healthy。
- 宿主实际连接 app 和 migrator：app 无 superuser/createdb/createrole/schema CREATE，
  migrator 非超级用户但可在自己的 schema 建表；Valkey authenticated PING 真实成功。
- 仅在该隔离 PostgreSQL 中为持久层测试创建独立 `enterprise_store_test` 与
  `enterprise_migration_test`，根 API 测试另用 `holmes`，数据互不混入。
- 实际解析后的 Compose 创建前校验通过，未显示任何 URL/密码/Key。

## 完整栈与保留的失败记录

镜像初次真实构建执行 npm ci、vue-tsc/Vite 编译及 Poetry 1.8.5 主锁安装，企业固定依赖
另行安装并通过 pip check。后续构建复用这些已执行的依赖/前端层，没有修改主锁文件。

- 初次完整栈的 API `/ready` 已返回 200、PG 心跳真实存在，但 worker 健康命令导入 Holmes
  超过 10 秒并导致启动等待失败；记录保存在
  [startup-first-health-failure-20261010.json](reports/startup-first-health-failure-20261010.json)。
  改为固定内部 `/ready` 的标准库探针，只清理失败项目的 owned 资源，保留运行目录。
- 随后的 `holmes-enterprise-c63b69b8be5f` 首次完整启动实际耗时 259.807 秒，六个持续服务
  healthy、一次性迁移 exit 0。原迁移容器曾继承 API 健康探针，故历史 health 显示 unhealthy，
  已在 Compose 明确关闭一次性任务健康探测，不能把该旧状态隐去。
  [startup-final-20261010.json](reports/startup-final-20261010.json) 保留首次镜像/启动记录。
- 原实栈队列/RBAC/重复消息/复核闭环 25 个检查通过，耗时 86.219 秒；
  [真实队列报告](../reports/enterprise-live-final-20261010.json) 使用 `.invalid` 不可用数据源，
  首次任务 partial、4 次工具调用、2.797 秒，没有连接真实 ERP 或调用模型。
- 监控初次采集遭遇 Host 校验 400；内部未发布 GET-only relay 修正后实际得到 503，
  进一步定位 PostgreSQL 指标聚合参数问题。记录保存在
  [metrics-first-failure-20261010.json](reports/metrics-first-failure-20261010.json)。
  前端首次资源审计检索也真实失败。两处应用修复已在 SQLite 与独立真实 PostgreSQL 各 7 项
  验证通过，再以新的镜像更新运行栈，原失败报告不覆盖。
- 真实 pg_dump → 全新空数据库恢复 9 项通过，79.900 秒；8 个业务记录、29 个审计记录、
  3 个账号保留，目标旧会话撤销为 0、源 2 个会话保留，1547 个既有 v1 runtime 文件哈希不变。
  [backup-restore-final-20261010.json](reports/backup-restore-final-20261010.json) 不包含 dump 或凭据。

## 构建和部署策略验证

52 项独立测试实际通过，12.61 秒，覆盖创建前 manifest、标签/资源 ID/挂载范围、同名未标记
资源拒绝、恢复目标保护、升级准入竞争、旧配置保留及 Dockerfile/.dockerignore 链接/结点拒绝。
命令为 `python -m pytest examples/erpnext-support/enterprise/tests -q -o addopts= --noconftest`；
[JUnit](reports/deployment-upgrade-policy-verified-20261010.xml) 记录实际结果。
首次未清除上游 addopts 的调用虽 52 个断言通过，但继承全 Holmes 46% 覆盖率门槛而退出失败；
[首次 XML](reports/deployment-upgrade-build-policy-final-20261010.xml) 保留，不将其退出状态称为成功。
这是独立部署模块测试，不据此宣称全项目覆盖率。

最终补丁构建镜像 `holmes-erp-enterprise:20261010-39edc000881c`，构建 exit 0；
OCI manifest 为 `sha256:6863aa62c947b0bba735fb5c87acad3116a151041154f51c3a928d087908eb8a`。
[新源码哈希清单](reports/source-manifest-upgrade-20261010.json) 保留 312 个输入哈希，
明确使用 `cb7716da9` 加修复提交 `e51bd9cfd`，不声称旧提交包含后来补丁。

## 保留数据升级和最终运行核验

实际有数据升级 12 项通过，244.817 秒，
[upgrade-final-20261010.json](reports/upgrade-final-20261010.json)。旧配置字节保持不变，
DB/queue/gateway/Prometheus 容器 ID 和全部卷名保留；8 个业务记录、29 个审计记录、
3 个账号及密码哈希、2 个会话一致，13 个私有文件及 1547 个既有 v1 runtime 文件哈希不变。
新 migrate exit 0 且没有持续 Health 字段，新 API/worker healthy。

当前运行项目仍为 `holmes-enterprise-c63b69b8be5f`，后续操作使用
`.runtime/holmes-enterprise-c63b69b8be5f/config-version-7fd288c93ab6.json`；
旧 `config.json`、原镜像和更新前私有备份均保留，登录文件未覆盖。

最终实栈只读检查 19 项通过，18.066 秒，
[deployment-upgrade-final-20261010.json](reports/deployment-upgrade-final-20261010.json)：
API health/ready 200、六个持续服务 healthy、迁移 exit 0、前端编译静态资源 200，
app 角色无 superuser/DDL、审计仅 SELECT/INSERT、队列运维命令拒绝。
Prometheus target 实际 up、PG 持久任务 Gauge 有值，内部无 token GET 403、非 GET 403，
公开 `/metrics` 404。容器内全部 Holmes 文件哈希等于构建上下文，运行 Python 源与前端
构建输入等于最终工作区冻结内容。
哈希核对共 312 个构建输入，其中运行 Python 文件 225 个、前端输入 29 个；
实际静态 JS 为 `index-DlF5ZcXA.js`。容器依赖实际为 Python 3.12.10、Celery 5.6.3、
redis 6.4.0、Alembic 1.18.4、prometheus-client 0.26.0、SQLAlchemy 2.0.51、pg8000 1.31.5、tzdata 2026.5。

上述控制器首轮把非 GET 拒绝误限定为 405，实际 nginx deny-all 返回 403 而断言失败；
没有放宽服务策略，修正断言为明确拒绝 403/405 后通过。
[首次控制器报告](reports/deployment-verification-relay-assertion-first-20261010.json) 保留实际状态。

界面复测及 root 顺序执行的 queue/worker 故障演练见根模块各自报告，不混加检查点数。
真实模型未调用。
旧演示资源和旧 runtime 保留；当前 `.invalid`/容器本地延迟 fixture 是明确的故障用例，
不能称为真实 ERP 联调。
