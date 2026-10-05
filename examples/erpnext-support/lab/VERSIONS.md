# 隔离 ERPNext 实验版本依据

此目录新增的是实验生命周期和只读 Frappe 扩展，不是 HolmesGPT 原有功能。原仓库许可证未改。

选择 ERPNext **15.85.1**，只对这一明确版本作适配。官方镜像实际读取的 Frappe 版本为
**15.87.0**。2026-10-05 已成功执行镜像拉取，并在 `--network none` 的临时容器中
读取 `frappe.__version__`、`erpnext.__version__` 与该镜像内 DocType 源文件。

| 镜像 | 实际拉取的摘要 |
| --- | --- |
| frappe/erpnext:v15.85.1 | sha256:e1eb74c050518a5c481b6f1312b7c1d80ff97d3a366e00f01d0328c88d8781bf |
| mariadb:10.6.22 | sha256:7589b386fa6686a5d5f131b504ed21c81e492c48b78c6c4be0d5be1066542559 |
| redis:6.2.18-alpine | sha256:3211c33a618c457e5d241922c975dbc4f446d0bdb2dc75694f5573ef8e2d01fa |

版本和服务拓扑依据：

- [ERPNext 15.85.1 官方发行](https://github.com/frappe/erpnext/releases/tag/v15.85.1)
- [官方 frappe_docker 单 Compose 部署说明](https://github.com/frappe/frappe_docker/blob/main/docs/single-compose-setup.md)
- [官方容器服务配置](https://github.com/frappe/frappe_docker/blob/main/pwd.yml)：此文件目前指向 v16，
  本实验只复用服务拓扑，明确固定 v15 镜像，并改为随机凭据、项目范围和回环 8090 端口。
- [该版本 Delivery Note 字段](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/doctype/delivery_note/delivery_note.json)
- [该版本 Sales Order 字段](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/selling/doctype/sales_order/sales_order.json)
- [库存权威方法](https://github.com/frappe/erpnext/blob/v15.85.1/erpnext/stock/utils.py)

历史库存调用 `erpnext.stock.utils.get_stock_balance`，依靠该版本
`get_previous_sle` 的 `qty_after_transaction`，未自行求和、也未用当前 Bin 代替历史时点。
原方法检查 Item 的读取权限；新增 GET-only 扩展额外检查发货单、精确子行归属、
物料及仓库文档权限、Stock Ledger Entry 读取权限。不使用 `ignore_permissions`。
本版只允许已实际验证的 Company/Warehouse User Permission 组合；其他 User Permission
维度或定制 Stock Ledger Entry 权限钩子拒绝聚合，避免原生 SQL 跳过额外用户范围。
`get_bin` 会在缺失时创建 Bin，因此未用于诊断查询。

新扩展不改库存、单据或角色。初始化、提交测试单据、失败提交尝试和故障注入仅在
`scripts/` 与 `lab.py` 测试生命周期内。业务种子包含已提交的正常发货单、真实缺货草稿、
历史入库晚于发货时点的草稿及正常库存的未提交草稿；最终实际结果记录在 runtime 的
`seed-result.json`，不能以此文件的创建计划冒充验证成功。

ERPNext 使用 GPL-3.0，Frappe 使用 MIT；镜像中的这些项目独立遵循其上游许可证。
已在该实际镜像读取 `apps/erpnext/license.txt` 与 `apps/frappe/LICENSE` 确认。
本目录只调用上游方法，没有将上游实现复制并改称 HolmesGPT 的许可。
