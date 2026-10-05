# 只读适配边界与官方源码核验

仅支持真实官方容器核验的 ERPNext **15.85.1** / Frappe **15.87.0**。
本目录为新增适配器，没有复制 ERPNext/Frappe 实现代码。ERPNext 上游为 GPLv3，
Frappe 上游为 MIT；两者的许可与 HolmesGPT 的许可分别保留，不能互相替代。

## 允许的读取

|接口|用途与检查|业务写入审核|
|---|---|---|
|`GET /api/resource/<固定 DocType>/<已绑定单据名>`|Frappe `read_doc`检查文档读取权限并应用字段权限；适配器再做固定字段投影。单据只从任务发货单及其已读取链接展开。|不传`run_method`、`expand_links`；禁止文档方法调用。|
|`GET /api/resource/<固定 DocType>`|Frappe列表API经`frappe.client.get_list`/`frappe.get_list`遵守权限；只允许固定字段、过滤、排序及最多101条原始结果。|没有新增、修改、删除、提交或取消调用。|
|`GET holmes_readonly.api.capabilities`|新增服务端扩展返回实际版本、只读权限、安装扩展及限制。|认证且GET限定；只查询，不授予权限。|
|`GET holmes_readonly.api.stock_evidence`|新增扩展绑定发货单真实子行，检查发货单、Item、Warehouse文档权限与SLE读权限，再调用原生历史库存方法。|只查询，不调用`get_bin`或任何重算/预留操作。|

连接地址与认证由服务端绑定。Agent不能传URL、SQL、Shell、环境或新的发货单。
工具和每次HTTP读取都重新执行工作台提供的环境授权回调。

## 数量、批次与预留的核验依据

- [`stock/utils.py`，固定15.85.1](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/utils.py)：
  `get_stock_balance`查询`get_previous_sle`的`qty_after_transaction`。`get_bin`可能创建Bin，故不使用。
- [`stock/stock_ledger.py`](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/stock_ledger.py)：
  原生历史方法排除取消流水，按过账时间与创建时间排序，不以手写流水求和替代。
  已提交发货单会影响过账时点数量，因此缺货规则仅用于普通草稿出库。
- [`delivery_note.py`](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/doctype/delivery_note/delivery_note.py)：
  验证关联销售订单与有效预留仓库。只读账号可能看不见其他预留，故可见子集的仓库冲突只列候选原因。
- [`stock_reservation_entry.py`](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/doctype/stock_reservation_entry/stock_reservation_entry.py)：
  有效预留检查排除Delivered/Cancelled，记录预留数量及已交付数量。没有调用其更新、自动分配或释放方法。
- [`serial_and_batch_bundle.py`](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/doctype/serial_and_batch_bundle/serial_and_batch_bundle.py)：
  v15使用批次包，批次选择涉及当前日期与库存语义。适配器仅读取批次/包元数据；不宣称覆盖完整历史批次可用量或序列号冲突。
- [`Frappe v15.87.0 API v1`](https://github.com/frappe/frappe/blob/v15.87.0/frappe/api/v1.py)、
  [`client.py`](https://github.com/frappe/frappe/blob/v15.87.0/frappe/client.py)：核查资源读API与权限处理。

诊断账号的读取权限不能证明故障用户的提交权限。自定义工作流、库存维度、未适配扩展、
重算中或证据快照发生变化时，不确认数量根因。批次自动分配与历史批次量尚未适配。
没有把历史已修复缺陷硬编码为本版本的已知故障。

## 数据最小化

HTTP拒绝重定向、忽略系统代理配置、使用TLS验证并限制响应字节数。
文档中联系人、地址、客户名称、价格和财务字段不进入结果；日志再脱敏密码、Token、邮箱和手机号。
错误只返回固定错误码与已审核接口路径，不返回原始异常、认证头或完整API错误体。
工具返回采集时间与业务时点，证据编号由规则程序根据实际工具调用产生。
