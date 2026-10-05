# 实际测试与验收记录

本轮在2026-10-05至10-06（北京时间）实际执行。主机 Windows、独立Python3.12.10，Docker29.8.1/Compose5.5.1，Node24.21.0；ERPNext15.85.1/Frappe15.87.0，数据库MariaDB10.6.22、Redis6.2.18。模型未调用；所有真实业务结果是**规则降级诊断**，不表示模型准确率、商业效果或生产安全审核。

## 分层记录

| 层级 | 内容 | 已核对的执行结果 |
|---|---|---|
| A/B | 工作台账号/存储/授权/工单状态与Mock任务API | 43通过，100.28秒；`workbench-unit-snapshot-fixed.xml` |
| A/B | ERP适配契约、规则、原Agent循环和独立安全回归 | 最终147通过，45.48秒，`adapter-rules-engine-runtime-verified-20261006.xml`；14个ScriptedMockLLM场景仅证明原循环接线 |
| A/B | Vue组件/页面状态（Mock HTTP明确标注） | 32通过；生产bundle实际构建成功 |
| A/B | ERP实验生命周期与权限防护（Mock Docker） | 43通过；实际ERP API验证另列 |
| A/B | Docker客户部署准备逻辑 | 9通过；实际构建/容器另列 |
| C | 真实ERP API→原Tool.invoke→业务规则 | 12通过，66.24秒；不Mock ERP，不调用模型 |
| C补验 | 最终安全代码下正常/缺货原引擎路径 | 2通过，48.22秒（含Python导入），`erp-adapter-live-cd618083a6a645b9aee98fd4876d98ff.json`；正常单2.041秒6/6，缺货6.098秒6/6 |
| C | 真实受限collector/DB/Redis/应用停止与恢复 | 5通过，235.86秒；`runtime-live-final.xml`及对应实际JSON；仅操作拥有的实验项目，三类故障均恢复ready |
| D | Edge154.0.4258.53 headless，真实页面/API/ERP | 扩展21项断言通过，72.000秒，浏览器错误0；7/7采证成功，调查8.585秒，diagnosis/review仍partial、rules_degraded，business_complete=true，人工关闭没有例外覆盖 |
| 部署 | 固定依赖Docker镜像及8766独立实例 | 实际镜像构建成功；health/login/session/compiledFrontend/empty-environments通过。最终源码快照复核见deploy实际报告 |
| 维护 | 工作台SQLite CLI备份、完整性校验、恢复新文件 | 执行记录见 `backup-restore-*.json`；不覆盖旧库、未操作ERP数据 |

最后冻结源码的容器smoke已通过89.177秒（含启动）：镜像 `holmes-erp-workbench:20261006-ad26c4235286`，manifest摘要 `sha256:17afe00d629745bae12815a5e91048ce269f0efeb25618caaec52a979bd3231f`；实际HTML/JS/CSS、登录、会话、空态与Docker healthy通过。宿主最终版本另执行HTTP工单实采7/7、9.569秒，见 `final-workbench-live-4f4e4dc9187b48ce9b28f5cc32916d1c.json`；其partial与模型未调用状态保留。

不能将 A/B 的数量称为真实业务案例数量。D 的21项断言属于同一完整流程的21个检查点，不能称21个独立发货业务。

共享首版脱敏模块针对长输入的修复，已有4个focused回归通过40.86秒；新增长日志/重复关键字/中文联系方式与秘密边界包含在147项最终回归内。旧模式4k长词实测2.274秒，修复后0.0014秒；根工作台12k分段长输入另实测0.0095秒。敏感业务标识用服务端密钥派生稳定HMAC别名以避免不同物料脱敏后错误合并，不把它称匿名化保证。

主要真实ERP报告：`erp-adapter-live-3fba3e081a644db3950f38e0c0c76109.json`。该文件记录12场景实际证据，pytest判定12通过；JSON中的观测条目不自行推定测试全部通过。

| 真实场景 | 调查耗时 | 工具成功率 | 实际结论 |
|---|---:|---:|---|
| 已提交正常发货 | 2.111秒 | 6/6 | 实际对应未取消出库流水与单据齐全，业务提交完成 |
| 缺货草稿 | 2.188秒 | 6/6 | 当前与过账时点余额0；实际提交报NegativeStockError |
| 历史不足草稿 | 1.945秒 | 6/6 | 历史0、当前10；原生权威查询与晚于发货时点的入库证据 |
| 正常库存草稿 | 2.492秒 | 6/6 | 尚未提交；不误报为缺货，也不当成已完成发货 |
| 缺Error Log权限 | 2.469秒 | 5/6 | 仍给业务缺货规则结果，同时明确日志缺失 |
| 无业务读取权限 | 0.938秒 | 1/6 | diagnostic_permission，不伪造库存或故障用户提交权限 |
| 仓库范围受限单据 | 0.961秒 | 1/6 | 权限拒绝，不泄漏受限库存 |
| 独立人工补货并提交后重新读取 | 1.964秒 | 6/6 | 实际SLE与提交单据齐全，business_complete=true |

运行层实际结果另见 `runtime-live-b39fe449eea24be19edaea11db4147ba.json`：健康基准5.606秒、7/7；数据库停止4.332秒、1/5；缓存停止44.250秒、6/7；应用backend停止35.629秒、1/5。故障时的低成功率是预期观测，不能算成工具全部正常。三个故障均得到对应运行状态的引用候选，并在finally恢复到API ready；容器停止不单独证明业务根因。

真实人工处理脚本创建独立 `LAB-RECHECK` 和第五张发货单：此前0库存、提交实际失败，实验管理员补货5、明确更新业务时点、提交后出库3、余额2。它没有改动原四个基准案例。该写脚本从未注册到Agent。

## 至少30个独立预期场景

以下是有独立断言的代表场景，源码还包括参数化边界；各测试层级清楚分开：

| 编号/层级 | 场景 | 独立预期 |
|---|---|---|
|01 A|当前不足与历史不足|按两个权威余额区分原因|
|02 A|没有库存权威来源|负流水片段不成为数量根因|
|03 A|库存维度/重算/扩展障碍|禁止确认标准库存根因|
|04 A|已提交单据后的余额|不拿出库后余额误判提交前缺货|
|05 A|单据及精确出库流水|两者齐全才通过业务完成|
|06 A|已取消流水|不能证明业务完成|
|07 A|已取消单据|单据状态问题|
|08 A|无效关联销售订单|固定版本的前置条件判断|
|09 A|未知订单状态|不编造无效业务状态|
|10 A|两次读取modified变化|撤回跨快照数量结论|
|11 A|同物料同仓库多行|按stock_qty合并需求|
|12 A|时区无法核验|不确认历史数量原因|
|13 A|发货明细截断|仅描述可见合计，不确认全单根因|
|14 A|退货/非库存/批次序列号|不套用普通出库缺货规则|
|15 A|允许或未知负库存设置|只能作为候选|
|16 A|诊断读取权限不足|不推断故障用户可提交|
|17 A|API读失败|只能为运行候选，不能指认DB/Redis|
|18 A|伪造version_supported|仍需精确实际版本|
|19 A|可见预留仓库冲突|仅候选，说明不可见范围|
|20 A|批次今天过期|不推断历史故障时已经过期|
|21 A|缺数量/缺仓库公司|不填0，不虚构公司冲突|
|22 B|工具缺授权回调|构造拒绝|
|23 B|凭据序列化|schema/Toolset无密钥|
|24 B|响应DocType/单据名变化|拒绝身份错配|
|25 B|跨单子行|不查询其他单据库存|
|26 B|权威、仓库或业务时间变化|拒绝库存scope错配|
|27 B|HTTP慢源/超时/过大|有界失败，不采纳迟到结果|
|28 B|未注册工具/URL/SQL参数|原执行器拒绝，后台不执行|
|29 B|虚构证据编号|模型候选被拒绝|
|30 B|模型后续失败|先前实际调用与规则结论保留|
|31 B|并行批次达到预算|已提交本轮全部结果保留|
|32 B|调查中撤销环境授权|停止新读取并保存拒绝审计|
|33 A/B|账号/CSRF/越权角色|服务端拒绝读取或修改|
|34 A/B|重启与保留期限|中断明确、步骤保留；到期证据清理|
|35 C|真实正常/缺货/历史/草稿|真实读库证据与不同结论|
|36 C|真实最小权限拒绝/缺日志|不泄漏库存、允许明确业务降级|
|37 C|真实人工处理后复核|重新读单据及实际出库流水|
|38 C|DB/Redis/backend停止|引用运行证据的候选，恢复同一实验|
|39 D|页面工单完整闭环|新建、调查、证据、动作、重读、人工关闭|
|40 D|案例/环境真实CRUD与拒绝|人工审核、编辑撤审、真实403/空态|

## 修复过的失败与未执行项

保留失败记录：首个ERP接口权限验证15/19，修复后19/19；人工复核的非补零秒数导致scope错配，规范化受限时钟后12/12；工单任务完成读快照race，事务快照修复后43/43；浏览器两处Element Plus隐藏checkbox定位失败，改为实际label点击后21项通过；collector缺Health字段的formatter错误、逐字节过密授权导致超时，修正后页面实际collector成功；容器自定义frontend字符串配置导致500，修复Path转换并用新实例重验。旧报告不覆盖为“通过”。

仅按实际测量报告调用成功率和耗时；未获取真实模型用量或计费信息，因此真实模型准确率、token用量、费用、人工效率提升均**未测量**。原Agent循环的ScriptedMockLLM测试只能证明架构接线及约束。

同一组正常/缺货/历史/草稿案例可比较：固定规则已实际执行；本项目带工单/只读专项适配路径也实际执行且保留证据；未加ERP适配的HolmesGPT及真实外部模型对照**未执行**，没有凭据/经授权模型，不虚构对照效果。当前版本的真实批次/预留异常、真实已知软件缺陷、客户自定义工作流、外部模型流式取消与真实客户环境也未验收。

## 重复执行

```powershell
Set-Location E:\ai\holmesgpt
.\.venv\Scripts\python.exe -m pytest -c tests/erpnext_support/pytest.ini --confcutdir=tests/erpnext_support tests/erpnext_support -m 'not erp_live'
$env:ERP_SUPPORT_RUN_LIVE='1'
$env:ERP_SUPPORT_LIVE_PROJECT='填入本次拥有的holmes-erp项目'
.\.venv\Scripts\python.exe -m pytest -c tests/erpnext_support/pytest.ini --confcutdir=tests/erpnext_support tests/erpnext_support/test_erp_live.py
# 显式允许该隔离项目的停止/恢复；不要与浏览器或其他真实ERP测试同时运行。
$env:ERP_SUPPORT_RUN_RUNTIME_LIVE='1'
.\.venv\Scripts\python.exe -m pytest -c tests/erpnext_support/pytest.ini --confcutdir=tests/erpnext_support tests/erpnext_support/test_runtime_live.py
Set-Location examples\erpnext-support\frontend
npm test
npm run test:e2e -- --environment-id '实际环境ID' --delivery-note '实际已提交发货单'
```

不开启实测开关时 C 测试会明确skip；不能将skip写成通过。测试输出中现有Click/Starlette弃用警告保留，不在本轮无差别升级主依赖。
