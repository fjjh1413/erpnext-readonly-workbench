"""Deterministic conclusions with program-owned evidence IDs; no model assertions.

Historical quantities are accepted only from the reviewed native stock authority.
Ledger rows corroborate timing and posting, never replace stock semantics by a sum.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from holmes.plugins.toolsets.erpnext_support.models import ERP_VERSION, FRAPPE_VERSION, IDENTIFIER_FIELDS, RuleFinding, sanitize

TOLERANCE = Decimal("0.000001")
BARRIERS = (
    "inventory_dimensions", "custom_workflow", "stock_reposting", "document_changed",
    "additional_apps", "missing_read_permission:Inventory Dimension",
    "missing_read_permission:Workflow", "missing_read_permission:Repost Item Valuation",
    "库存维度", "自定义工作流", "未适配的安装扩展", "站点时区", "同一快照",
)


def number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def ambiguous_identifiers(value: Any) -> bool:
    """Legacy generic redaction cannot preserve distinct business identities."""
    if isinstance(value, dict):
        return any(
            key in IDENTIFIER_FIELDS and isinstance(item, str)
            and any(marker in item for marker in ("[REDACTED", "[EMAIL]", "[PHONE]"))
            or ambiguous_identifiers(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(ambiguous_identifiers(item) for item in value)


def build_report(calls: list[Any]) -> dict[str, Any]:
    """Consume actual upstream ToolCallResult objects or their JSON serialization."""
    evidence = []
    facts = []
    causes = []
    missing = []
    limits = []
    seen_causes = set()

    def fact(text: str, refs: list[str]) -> None:
        facts.append({"text": text, "evidence_ids": refs})

    def cause(code: str, status: str, text: str, refs: list[str]) -> None:
        key = (code, text)
        if key not in seen_causes:
            seen_causes.add(key)
            causes.append(RuleFinding(code=code, status=status, text=text, evidence_ids=refs).model_dump())

    for index, call in enumerate(calls, start=1):
        value = call.model_dump(mode="json") if hasattr(call, "model_dump") else call
        if not isinstance(value, dict):
            raise ValueError("Evidence calls must be structured tool records.")
        result = value.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("Evidence results must be structured.")
        data = result.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = {"payload": {}, "limitations": ["工具结果不是有效 JSON，不能作业务证据。"]}
        if not isinstance(data, dict):
            data = {"payload": {}, "limitations": ["工具结果结构无效。"]}
        entry = {
            "id": f"E{index:03d}", "tool_call_id": value.get("tool_call_id"),
            "tool_name": value.get("tool_name"), "status": result.get("status", "error"),
            "source": data.get("source", value.get("tool_name", "unknown")),
            "query": data.get("query", result.get("params") or {}),
            "collected_at": data.get("collected_at"), "business_at": data.get("business_at"),
            "payload": data.get("payload", {}), "limitations": data.get("limitations", []),
            "elapsed_seconds": result.get("elapsed_seconds"),
        }
        evidence.append(sanitize(entry))
        limits.extend(str(item) for item in entry["limitations"])
        if entry["status"] != "success":
            failure = entry["payload"].get("failure", {})
            code = failure.get("code") or result.get("error") or "tool_error"
            missing.append(f"{entry['tool_name']} 未取得可用证据：{code}。")
            if code in ("permission_denied", "authentication_denied", "authorization_revoked"):
                fact("诊断读取被权限或授权检查拒绝；这不代表故障操作用户的提交权限。", [entry["id"]])
                cause("diagnostic_permission", "confirmed", "诊断证据源权限不足，需要授权管理员检查只读账号范围。", [entry["id"]])
            elif code in ("source_timeout", "source_unavailable", "tool_timeout"):
                cause("runtime_unavailable", "candidate", "ERP API 读取不可用；单次读取故障不能确认数据库、Redis或容器根因。", [entry["id"]])

    successful = [entry for entry in evidence if entry["status"] == "success" and isinstance(entry["payload"], dict)]
    capabilities = [entry for entry in successful if entry["tool_name"] == "erp_capabilities"]
    admitted = bool(capabilities and capabilities[-1]["payload"].get("version_supported") is True and capabilities[-1]["payload"].get("erpnext_version") == ERP_VERSION and capabilities[-1]["payload"].get("frappe_version") == FRAPPE_VERSION)
    if capabilities:
        caps = capabilities[-1]
        fact(f"采集版本为 ERPNext {caps['payload'].get('erpnext_version')} / Frappe {caps['payload'].get('frappe_version')}。", [caps["id"]])
        for name, allowed in caps["payload"].get("read_permissions", {}).items():
            if allowed is not True:
                missing.append(f"诊断账号缺少 {name} 读取权限。")
    if not admitted:
        missing.append("没有成功核验受支持的固定 ERPNext/Frappe 组合，业务根因无法确认。")
    missing.append("未以故障操作用户验证提交权限、工作流角色及原始请求上下文。")
    limits.extend(["诊断保持 ERP 只读，人工处理与关闭工单由授权人员完成。", "API 可用不证明数据库、Redis、容器或物理发货完成；运行层采集尚未接入。", "未匹配经人工审核且满足版本条件的已知缺陷案例，不推断当前版本存在历史缺陷。"])

    contexts = [entry for entry in successful if entry["tool_name"] == "erp_delivery_context"]
    note = contexts[-1]["payload"].get("delivery_note", {}) if contexts else {}
    context_id = contexts[-1]["id"] if contexts else None
    changed = len({entry["payload"].get("delivery_note", {}).get("modified") for entry in contexts}) > 1
    stock_entries = [entry for entry in successful if entry["tool_name"] == "erp_stock_evidence"]
    if note and any(entry["payload"].get("delivery_note_modified") != note.get("modified") or entry["payload"].get("snapshot_consistent") is not True for entry in stock_entries):
        changed = True
    if changed:
        missing.append("调查期间发货单 modified 发生变化，必须重新采集一致的单据与库存证据。")
        limits.append("证据快照不一致，本次不确认库存、预留或批次业务根因。")
    if not note:
        missing.append("未读取到发货单，无法检查业务状态和明细。")
    elif context_id:
        fact(f"发货单 {note.get('name')} docstatus={note.get('docstatus')}，状态={note.get('status')}，过账={note.get('posting_date')} {note.get('posting_time')}。", [context_id])
        if note.get("items_truncated"):
            missing.append("发货明细已截断，当前分析不覆盖完整单据。")
        if note.get("is_return"):
            limits.append("退货业务暂未适配，不套用普通出库缺货规则。")
        if note.get("docstatus") == 0:
            fact("发货单仍为草稿，不能据此确认业务已完成。", [context_id])

    ambiguous = any(ambiguous_identifiers(entry["payload"]) for entry in successful)
    if ambiguous:
        missing.append("业务标识仅有通用脱敏占位，无法核验物料、仓库或单据关联，需重新采集稳定别名证据。")
    safe_business = admitted and note and not changed and not ambiguous
    if safe_business and note.get("docstatus") == 2:
        cause("document_state", "confirmed", "发货单已取消，不能作为普通草稿继续提交；需要人工核查取消或修订链路。", [context_id])
    if safe_business and not note.get("is_return"):
        for context_entry in contexts[-1:]:
            for order in context_entry["payload"].get("sales_orders", []):
                if order.get("status") in ("Closed", "On Hold", "Cancelled") or order.get("docstatus") in (0, 2):
                    cause("document_precondition", "confirmed", f"关联销售订单 {order.get('name')} 状态 {order.get('status')} / docstatus {order.get('docstatus')} 不满足普通发货前置条件。", [context_entry["id"]])

    stock_by_line = {}
    for entry in stock_entries:
        payload = entry["payload"]
        line_name = payload.get("line_name")
        stock_by_line[line_name] = entry
        if not safe_business:
            continue
        item = payload.get("item", {})
        warehouse = payload.get("warehouse_info", {})
        refs = [context_id, entry["id"]]
        if item.get("disabled") == 1:
            cause("document_precondition", "confirmed", f"物料 {payload.get('item_code')} 已停用。", refs)
        if warehouse.get("disabled") == 1 or warehouse.get("is_group") == 1 or (warehouse.get("company") is not None and note.get("company") is not None and warehouse["company"] != note["company"]):
            cause("document_precondition", "confirmed", f"仓库 {payload.get('warehouse')} 停用、为组仓库或与单据公司不一致。", refs)
        if note.get("docstatus") != 0 or note.get("is_return") or item.get("is_stock_item") != 1:
            continue
        if item.get("has_batch_no") or item.get("has_serial_no") or any(any(marker in str(limit) for marker in BARRIERS) for limit in entry["limitations"] + limits):
            missing.append(f"明细 {line_name} 有未适配维度、批次、序列号或重算条件，汇总数量不能确认可发货性。")
            continue
        if payload.get("authority") != "erpnext.stock.utils.get_stock_balance":
            missing.append(f"明细 {line_name} 的历史库存没有经过核验的权威来源。")
            continue
        matching = [row for row in note.get("items", []) if row.get("item_code") == payload.get("item_code") and row.get("warehouse") == payload.get("warehouse")]
        quantities = [number(row.get("stock_qty")) for row in matching]
        if not quantities or any(quantity is None or quantity <= 0 for quantity in quantities):
            missing.append(f"明细 {line_name} 缺少有效的正数库存单位数量。")
            continue
        required = sum(quantities, Decimal(0))
        current = number(payload.get("current_qty"))
        historical = number(payload.get("posting_qty"))
        if current is None or historical is None:
            missing.append(f"明细 {line_name} 的权威数量结果不完整。")
            continue
        quantity_scope = "已读取明细合计" if note.get("items_truncated") else "本单合计"
        fact(f"物料 {payload.get('item_code')} / 仓库 {payload.get('warehouse')} {quantity_scope}需 {required} 库存单位；当前量 {current}，过账时点量 {historical}。", refs)
        negative_allowed = payload.get("allow_negative_stock") is True or item.get("allow_negative_stock") == 1
        negative_unknown = "allow_negative_stock" not in payload
        severity = "candidate" if negative_allowed or negative_unknown or note.get("items_truncated") else "confirmed"
        if current + TOLERANCE < required:
            cause("stock_shortage", severity, f"物料 {payload.get('item_code')} 在仓库 {payload.get('warehouse')} 当前库存少于本单所需数量。", refs)
        elif historical + TOLERANCE < required:
            cause("historical_stock_shortage", severity, f"当前库存足够，但物料 {payload.get('item_code')} 在发货单过账时点库存不足。", refs)
            if any(number(row.get("actual_qty")) is not None and number(row.get("actual_qty")) > 0 and str(row.get("posting_datetime", "")) > f"{note.get('posting_date')} {note.get('posting_time')}" for row in payload.get("ledger", [])):
                fact("已读取到发货单过账时点之后的正向库存流水；不使用这些流水片段求和替代权威查询。", [entry["id"]])
        if negative_allowed or negative_unknown:
            missing.append("允许负库存的条件未排除，数量不足本身不能证明提交失败的原因。")
        if note.get("items_truncated"):
            missing.append("只核对了可见明细数量，不能据此确认完整发货单的库存根因。")

    for entry in [value for value in successful if value["tool_name"] == "erp_reservation_batch"]:
        payload = entry["payload"]
        if not safe_business or note.get("is_return"):
            continue
        line = payload.get("line", {})
        reservations = payload.get("reservations", [])
        active = [row for row in reservations if row.get("docstatus") == 1 and row.get("status") not in ("Delivered", "Cancelled")]
        if line.get("against_sales_order") and line.get("so_detail") and active:
            warehouses = {row.get("warehouse") for row in active}
            if line.get("warehouse") and line.get("warehouse") not in warehouses:
                # A bounded authorized subset cannot prove that no matching reserve
                # exists elsewhere. Report the mismatch as a candidate, not certainty.
                cause("reservation_warehouse", "candidate", f"已读到关联销售订单明细在其他仓库的有效预留；所选仓库 {line.get('warehouse')} 未见于当前可见结果。", [context_id, entry["id"]])
        for batch in payload.get("batches", []):
            if batch.get("item") != line.get("item_code") or batch.get("disabled") == 1:
                cause("batch_metadata_conflict", "candidate", f"所选批次 {batch.get('name')} 停用或所属物料不匹配。", [entry["id"]])
            if batch.get("expiry_date") and str(batch["expiry_date"]) < str(payload.get("checked_on", "")):
                fact(f"批次 {batch.get('name')} 在本次采集日已超过有效期；不能据此证明历史故障时已过期。", [entry["id"]])
                cause("batch_expired_now", "candidate", "当前所选批次存在有效期条件，需人工复核原故障日及该版本批次选择语义。", [entry["id"]])
        for bundle in payload.get("bundles", []):
            if bundle.get("item_code") != line.get("item_code") or bundle.get("warehouse") != line.get("warehouse") or bundle.get("is_cancelled") or bundle.get("voucher_type") != "Delivery Note" or bundle.get("voucher_no") != note.get("name"):
                cause("batch_bundle_conflict", "candidate", "已关联的序列号/批次包与当前发货单、物料或仓库不一致，需按 ERP 标准流程人工核查。", [entry["id"]])

    business_complete = False
    if safe_business and note.get("docstatus") == 1 and not note.get("is_return") and not note.get("items_truncated"):
        checks = []
        for line in note.get("items", []):
            entry = stock_by_line.get(line.get("name"))
            if entry is None:
                checks.append(False)
                continue
            if entry["payload"].get("item", {}).get("is_stock_item") == 0:
                checks.append(True)
                continue
            quantity = number(line.get("stock_qty"))
            matching = [row for row in entry["payload"].get("ledger", []) if row.get("voucher_type") == "Delivery Note" and row.get("voucher_no") == note.get("name") and row.get("voucher_detail_no") == line.get("name") and row.get("is_cancelled") == 0]
            checks.append(quantity is not None and any(number(row.get("actual_qty")) is not None and abs(number(row["actual_qty"]) + quantity) <= TOLERANCE for row in matching))
        business_complete = bool(checks and all(checks))
        if business_complete:
            fact("发货单已提交，且每条库存明细存在对应未取消的出库流水；这是 ERP 过账复核，仍需人工确认实际发货与工单关闭。", [context_id] + [value["id"] for value in stock_by_line.values()])
        else:
            missing.append("发货单已提交，但未读取到全部对应出库流水，不能仅凭状态确认库存发货业务完成。")

    priority = ("stock_shortage", "historical_stock_shortage", "document_precondition", "document_state", "reservation_warehouse", "batch_metadata_conflict", "batch_bundle_conflict", "batch_expired_now", "diagnostic_permission", "runtime_unavailable")
    classification = next((code for code in priority if any(value["code"] == code for value in causes)), "normal" if business_complete else "unable_to_confirm")
    report = {
        "classification": classification,
        "status": "business_complete" if business_complete else "manual_action" if causes else "needs_information",
        "engine": "verified_rules", "facts": facts, "causes": causes,
        "missing": list(dict.fromkeys(missing)), "limits": list(dict.fromkeys(limits)),
        "manual_steps": ["由授权实施顾问核对引用证据及原错误，不执行自动修复。", "按结论核查实际库存与过账时点、关联单据、预留仓库或批次条件；必要处理在 ERP 中由授权人员完成。"],
        "verification": ["人工处理后重新读取同一发货单、库存权威结果和对应未取消的出库流水。", "API 恢复或单据状态变化不能证明旧候选原因成立；对照原错误与业务结果，由售后人员人工确认关闭。"],
        "evidence": evidence, "evidence_inconsistent": changed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    ids = {entry["id"] for entry in evidence}
    if any(not set(value["evidence_ids"]).issubset(ids) for value in facts + causes):
        raise ValueError("A rule attempted to cite nonexistent evidence.")
    return sanitize(report)
