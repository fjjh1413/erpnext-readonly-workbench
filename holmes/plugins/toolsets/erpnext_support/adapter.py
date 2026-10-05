"""Original read-only wrappers around reviewed Frappe resource and guard APIs."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from urllib3.exceptions import ReadTimeoutError

from holmes.plugins.toolsets.erpnext_support.models import (
    CAPABILITIES_METHOD, DOCUMENT_TYPES, ERP_VERSION, STOCK_METHOD,
    ERPAccessError, ERPConfig, EvidenceEnvelope, document_name, normalize_posting_time,
)

DOC_FIELDS = {
    "Delivery Note": ("name", "docstatus", "status", "company", "posting_date", "posting_time", "is_return", "return_against", "modified", "amended_from"),
    "Sales Order": ("name", "docstatus", "status", "company", "delivery_status", "per_delivered", "modified", "reserve_stock"),
    "Item": ("name", "is_stock_item", "disabled", "has_batch_no", "has_serial_no", "stock_uom", "allow_negative_stock", "inspection_required_before_delivery"),
    "Warehouse": ("name", "company", "disabled", "is_group"),
    "Batch": ("name", "item", "disabled", "expiry_date", "manufacturing_date"),
    "Serial and Batch Bundle": ("name", "docstatus", "item_code", "warehouse", "voucher_type", "voucher_no", "voucher_detail_no", "is_cancelled", "type_of_transaction", "posting_date", "posting_time", "total_qty", "modified"),
}
LINE_FIELDS = ("name", "idx", "item_code", "warehouse", "qty", "stock_qty", "uom", "stock_uom", "conversion_factor", "against_sales_order", "so_detail", "batch_no", "serial_and_batch_bundle", "use_serial_batch_fields", "quality_inspection")
LIST_FIELDS = {
    "Bin": ("name", "item_code", "warehouse", "actual_qty", "reserved_qty", "reserved_stock", "modified"),
    "Stock Ledger Entry": ("name", "item_code", "warehouse", "posting_date", "posting_time", "posting_datetime", "actual_qty", "qty_after_transaction", "voucher_type", "voucher_no", "voucher_detail_no", "is_cancelled", "creation", "modified"),
    "Stock Reservation Entry": ("name", "item_code", "warehouse", "voucher_type", "voucher_no", "voucher_detail_no", "docstatus", "status", "reserved_qty", "delivered_qty", "reservation_based_on", "has_batch_no", "has_serial_no", "modified"),
    "Error Log": ("name", "creation", "method", "error", "reference_doctype", "reference_name", "trace_id"),
}
STOCK_FIELDS = ("delivery_note", "line_name", "delivery_note_modified", "item_code", "warehouse", "posting_date", "posting_time", "posting_qty", "current_qty", "authority", "current_as_of", "allow_negative_stock")
HTTP_FETCH_SLOTS = threading.BoundedSemaphore(16)


def project_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ERPAccessError("invalid_resource_shape")
    return {key: value[key] for key in fields if key in value}


class ERPAdapter:
    def __init__(self, config: ERPConfig | dict[str, Any], authorize: Callable[[], bool] | None = None):
        self.config = config if isinstance(config, ERPConfig) else ERPConfig.model_validate(config)
        self.authorize = authorize
        self.session = requests.Session()
        self.session.trust_env = False
        self._admitted = False
        self._tool_state = threading.local()

    @property
    def _deadline(self) -> float | None:
        return getattr(self._tool_state, "deadline", None)

    def begin_tool(self) -> None:
        self._tool_state.deadline = time.monotonic() + self.config.max_tool_timeout

    def check_authorization(self) -> None:
        if self.authorize is not None:
            try:
                allowed = self.authorize() is True
            except Exception:
                allowed = False
            if not allowed:
                raise ERPAccessError("authorization_revoked")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.check_authorization()
        if path.startswith("/api/resource/"):
            doctype = path[len("/api/resource/"):].split("/")[0]
            if doctype not in {quote(value, safe="") for value in DOCUMENT_TYPES}:
                raise ERPAccessError("endpoint_not_allowed")
        elif path not in (f"/api/method/{CAPABILITIES_METHOD}", f"/api/method/{STOCK_METHOD}"):
            raise ERPAccessError("endpoint_not_allowed")
        if params and set(params) - {"fields", "filters", "order_by", "limit_start", "limit_page_length", "delivery_note", "line_name"}:
            raise ERPAccessError("query_not_allowed")
        remaining = self.config.timeout_seconds
        if self._deadline is not None:
            remaining = min(remaining, self._deadline - time.monotonic())
            if remaining <= 0:
                raise ERPAccessError("tool_timeout", path)
        headers = {"Authorization": f"token {self.config.api_key.get_secret_value()}:{self.config.api_secret.get_secret_value()}", "Accept": "application/json"}
        deadline = time.monotonic() + remaining
        timeout_code = "tool_timeout" if self._deadline is not None and self._deadline <= deadline else "source_timeout"
        slots = HTTP_FETCH_SLOTS
        if not slots.acquire(blocking=False):
            raise ERPAccessError("source_unavailable", path)
        cancelled = threading.Event()
        outcome: list[Any] = []
        response_holder: list[Any] = []

        def fetch() -> None:
            response = None
            try:
                response = self.session.get(
                    self.config.base_url + path, params=params, headers=headers,
                    timeout=(min(3, remaining), remaining), allow_redirects=False, stream=True,
                )
                response_holder.append(response)
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise ERPAccessError(timeout_code, path)
                if 300 <= response.status_code < 400:
                    raise ERPAccessError("redirect_blocked", path)
                if response.status_code >= 400:
                    code = {401: "authentication_denied", 403: "permission_denied", 404: "not_found", 429: "rate_limited"}.get(response.status_code, "source_unavailable")
                    raise ERPAccessError(code, path)
                content = bytearray()
                # urllib3 read1 returns available bytes, avoiding a buffered
                # 4096-byte wait on a source that slowly drips a smaller body.
                read1 = getattr(getattr(response, "raw", None), "read1", None)
                iterator = None if callable(read1) else response.iter_content(chunk_size=4096)
                while True:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        raise ERPAccessError(timeout_code, path)
                    self.check_authorization()
                    chunk = read1(4096, decode_content=True) if callable(read1) else next(iterator, b"")
                    if not chunk:
                        break
                    content.extend(chunk)
                    if len(content) > self.config.max_response_bytes:
                        raise ERPAccessError("response_too_large", path)
                body = json.loads(content)
                if not isinstance(body, dict) or ("data" not in body and "message" not in body):
                    raise ERPAccessError("invalid_response_shape", path)
                self.check_authorization()
                if not cancelled.is_set() and time.monotonic() < deadline:
                    outcome.append(body["data"] if "data" in body else body["message"])
                else:
                    raise ERPAccessError(timeout_code, path)
            except ERPAccessError as error:
                outcome.append(error)
            except (requests.Timeout, ReadTimeoutError):
                outcome.append(ERPAccessError("source_timeout", path))
            except requests.RequestException:
                outcome.append(ERPAccessError("source_unavailable", path))
            except (ValueError, TypeError):
                outcome.append(ERPAccessError("invalid_response_json", path))
            except Exception:
                outcome.append(ERPAccessError("source_unavailable", path))
            finally:
                try:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
                finally:
                    slots.release()

        worker = threading.Thread(target=fetch, daemon=True, name="erp-readonly-fetch")
        try:
            worker.start()
        except Exception:
            slots.release()
            raise ERPAccessError("source_unavailable", path) from None
        worker.join(max(0, deadline - time.monotonic()))
        if worker.is_alive() or time.monotonic() >= deadline:
            cancelled.set()
            if response_holder:
                # Closing an active socket may itself block. Cleanup must not
                # extend the caller's deadline; a late result is never accepted.
                def close_response() -> None:
                    try:
                        response_holder[0].close()
                    except Exception:
                        pass
                threading.Thread(target=close_response, daemon=True, name="erp-readonly-close").start()
            raise ERPAccessError(timeout_code, path)
        self.check_authorization()
        if not outcome:
            raise ERPAccessError("source_unavailable", path)
        if isinstance(outcome[0], ERPAccessError):
            raise outcome[0]
        return outcome[0]

    def _document(self, doctype: str, name: str) -> dict[str, Any]:
        if doctype not in DOC_FIELDS:
            raise ERPAccessError("doctype_not_allowed")
        name = document_name(name)
        value = self._get(f"/api/resource/{quote(doctype, safe='')}/{quote(name, safe='')}")
        if not isinstance(value, dict) or value.get("name") != name:
            raise ERPAccessError("document_identity_mismatch", doctype)
        result = project_fields(value, DOC_FIELDS[doctype])
        if "posting_time" in result:
            result["posting_time"] = normalize_posting_time(result["posting_time"])
        if doctype == "Delivery Note":
            items = value.get("items")
            if not isinstance(items, list) or not all(isinstance(row, dict) for row in items):
                raise ERPAccessError("invalid_delivery_items", doctype)
            result["items"] = [project_fields(row, LINE_FIELDS) for row in items[:self.config.max_lines]]
            result["items_truncated"] = len(items) > self.config.max_lines
        if doctype == "Serial and Batch Bundle":
            entries = value.get("entries", [])
            if not isinstance(entries, list):
                raise ERPAccessError("invalid_bundle_entries")
            result["entries"] = [project_fields(row, ("batch_no", "qty", "warehouse", "is_outward")) for row in entries[:self.config.max_rows]]
            result["entries_truncated"] = len(entries) > self.config.max_rows
        return result

    def _list(self, doctype: str, filters: list[list[Any]], order_by: str = "modified desc") -> tuple[list[dict[str, Any]], bool]:
        if doctype not in LIST_FIELDS:
            raise ERPAccessError("doctype_not_allowed")
        value = self._get(f"/api/resource/{quote(doctype, safe='')}", {
            "fields": json.dumps(LIST_FIELDS[doctype]), "filters": json.dumps(filters),
            "order_by": order_by, "limit_start": 0, "limit_page_length": self.config.max_rows + 1,
        })
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ERPAccessError("invalid_list_shape", doctype)
        return [project_fields(row, LIST_FIELDS[doctype]) for row in value[:self.config.max_rows]], len(value) > self.config.max_rows

    def _envelope(self, source: str, query: dict[str, Any], payload: dict[str, Any], business_at: str | None = None, limitations: list[str] | None = None) -> dict[str, Any]:
        value = EvidenceEnvelope(
            source=source, query=self.config.sanitize(query), collected_at=datetime.now(timezone.utc),
            business_at=business_at, payload=self.config.sanitize(payload),
            limitations=self.config.sanitize(limitations or []),
        )
        return value.model_dump(mode="json")

    def capabilities(self) -> dict[str, Any]:
        self.check_authorization()
        raw = self._get(f"/api/method/{CAPABILITIES_METHOD}")
        if not isinstance(raw, dict):
            raise ERPAccessError("invalid_capabilities_shape")
        erp_version = raw.get("erpnext_version")
        frappe_version = raw.get("frappe_version")
        supported = erp_version == ERP_VERSION and self.config.expected_frappe_version is not None and frappe_version == self.config.expected_frappe_version
        permissions = raw.get("read_permissions", {})
        if not isinstance(permissions, dict):
            raise ERPAccessError("invalid_permissions_shape")
        limits = [str(value)[:500] for value in raw.get("limitations", [])[:20]]
        if not supported:
            limits.append("软件版本未与经过核验的固定 ERPNext/Frappe 版本完全匹配，业务根因规则停用。")
        apps = raw.get("installed_apps", [])
        if not isinstance(apps, list):
            raise ERPAccessError("invalid_installed_apps_shape")
        safe_apps = [project_fields(value, ("name", "version")) for value in apps[:20]]
        if any(value.get("name") not in ("frappe", "erpnext", "holmes_readonly") for value in safe_apps):
            limits.append("存在未适配的安装扩展，可能改变业务验证。")
        if raw.get("custom_workflows"):
            limits.append("存在自定义工作流，尚未验证其业务与权限条件。")
        if raw.get("inventory_dimensions"):
            limits.append("存在库存维度，当前历史数量规则未适配。")
        site_timezone = raw.get("site_timezone")
        if not site_timezone:
            limits.append("ERP 站点时区未核验，历史时点与日志窗口无法确认。")
        elif site_timezone != self.config.site_timezone:
            limits.append("ERP 站点时区与工作台绑定时区不一致。")
        self._admitted = supported
        return self._envelope(CAPABILITIES_METHOD, {}, {
            "erpnext_version": erp_version, "frappe_version": frappe_version,
            "version_supported": supported, "installed_apps": safe_apps,
            "read_permissions": {name: permissions.get(name) is True for name in DOCUMENT_TYPES},
            "site_timezone": site_timezone,
            "operator_submit_permission": "unknown",
            "runtime_health": "not_configured",
        }, limitations=limits)

    def _ensure_version(self) -> None:
        self.check_authorization()
        if not self._admitted:
            self.capabilities()
        if not self._admitted:
            raise ERPAccessError("unsupported_version")

    def delivery_context(self, delivery_note: str) -> dict[str, Any]:
        self._ensure_version()
        note = self._document("Delivery Note", delivery_note)
        limits = []
        if note["items_truncated"]:
            limits.append("发货明细超过任务上限，未读取完整单据。")
        order_names = list(dict.fromkeys(row.get("against_sales_order") for row in note["items"] if row.get("against_sales_order")))
        orders = []
        for name in order_names[:self.config.max_lines]:
            try:
                orders.append(self._document("Sales Order", name))
            except ERPAccessError as error:
                if error.code == "authorization_revoked":
                    raise
                limits.append(f"关联销售订单读取受限：{error.code}。")
        return self._envelope("Frappe resource: Delivery Note/Sales Order", {"delivery_note": delivery_note}, {
            "delivery_note": note, "sales_orders": orders,
        }, business_at=f"{note.get('posting_date', '')}T{note.get('posting_time', '')}", limitations=limits)

    def _bound_line(self, delivery_note: str, line_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        note = self._document("Delivery Note", delivery_note)
        line_name = document_name(line_name)
        # Resolve only among already bound, bounded child rows. Never decode an
        # arbitrary alias or use an Agent-supplied value as an ERP lookup target.
        matches = [row for row in note["items"] if row.get("name") == line_name
                   or self.config.sanitize({"name": row.get("name")})["name"] == line_name]
        if len(matches) != 1:
            raise ERPAccessError("line_outside_delivery_note")
        return note, matches[0]

    def stock_evidence(self, delivery_note: str, line_name: str) -> dict[str, Any]:
        self._ensure_version()
        note, line = self._bound_line(delivery_note, line_name)
        line_name = line["name"]
        item = self._document("Item", line.get("item_code"))
        warehouse = self._document("Warehouse", line.get("warehouse"))
        raw = self._get(f"/api/method/{STOCK_METHOD}", {"delivery_note": delivery_note, "line_name": line_name})
        if (
            not isinstance(raw, dict) or raw.get("delivery_note") != delivery_note
            or raw.get("line_name") != line_name or raw.get("item_code") != line.get("item_code")
            or raw.get("warehouse") != line.get("warehouse")
            or raw.get("posting_date") != note.get("posting_date")
            or normalize_posting_time(raw.get("posting_time")) != note.get("posting_time")
            or raw.get("authority") != "erpnext.stock.utils.get_stock_balance"
        ):
            raise ERPAccessError("stock_scope_or_authority_mismatch")
        limits = [str(value)[:500] for value in raw.get("limitations", [])[:20]]
        if raw.get("delivery_note_modified") != note.get("modified"):
            limits.append("库存查询期间发货单已变化，证据不能作为同一快照。")
        if item.get("has_batch_no") or item.get("has_serial_no"):
            limits.append("批次/序列号物料：汇总库存不能证明所选批次/序列号可发货。")
        stock = project_fields(raw, STOCK_FIELDS)
        stock["posting_time"] = note["posting_time"]
        stock.update({"item": item, "warehouse_info": warehouse, "required_stock_qty": line.get("stock_qty"), "delivery_note_docstatus": note.get("docstatus"), "is_return": note.get("is_return"), "snapshot_consistent": raw.get("delivery_note_modified") == note.get("modified")})
        # Ledger rows are corroboration only; native authority above determines quantity.
        try:
            ledger, truncated = self._list("Stock Ledger Entry", [
                ["item_code", "=", line["item_code"]], ["warehouse", "=", line["warehouse"]],
                ["is_cancelled", "=", 0],
            ], "posting_datetime desc, creation desc")
            if any(row.get("item_code") != line["item_code"] or row.get("warehouse") != line["warehouse"] or row.get("is_cancelled") != 0 for row in ledger):
                raise ERPAccessError("ledger_scope_mismatch")
            stock["ledger"] = ledger
            stock["ledger_truncated"] = truncated
            if truncated:
                limits.append("库存流水已截断，不以片段求和作为历史库存。")
        except ERPAccessError as error:
            if error.code == "authorization_revoked":
                raise
            stock["ledger"] = []
            limits.append(f"库存流水证据读取受限：{error.code}。")
        return self._envelope(STOCK_METHOD, {"delivery_note": delivery_note, "line_name": line_name, "item_code": line["item_code"], "warehouse": line["warehouse"]}, stock, f"{note.get('posting_date', '')}T{note.get('posting_time', '')}", limits)

    def reservation_batch(self, delivery_note: str, line_name: str) -> dict[str, Any]:
        self._ensure_version()
        note, line = self._bound_line(delivery_note, line_name)
        line_name = line["name"]
        limits = ["预留结果只覆盖诊断账号可见的授权范围，空列表不能证明没有隐藏预留。", "批次库存的自动分配与历史可用量未适配；不调用可能写入的批次重算或预留方法。"]
        reservations = []
        truncated = False
        if line.get("against_sales_order") and line.get("so_detail"):
            try:
                reservations, truncated = self._list("Stock Reservation Entry", [
                    ["voucher_type", "=", "Sales Order"], ["voucher_no", "=", line["against_sales_order"]],
                    ["voucher_detail_no", "=", line["so_detail"]], ["item_code", "=", line["item_code"]],
                    ["docstatus", "=", 1], ["status", "not in", ["Delivered", "Cancelled"]],
                ])
                if any(row.get("voucher_type") != "Sales Order" or row.get("voucher_no") != line["against_sales_order"] or row.get("voucher_detail_no") != line["so_detail"] or row.get("item_code") != line["item_code"] or row.get("docstatus") != 1 or row.get("status") in ("Delivered", "Cancelled") for row in reservations):
                    reservations = []
                    raise ERPAccessError("reservation_scope_mismatch")
            except ERPAccessError as error:
                if error.code == "authorization_revoked":
                    raise
                limits.append(f"预留记录读取受限：{error.code}。")
        bundles = []
        batch_names = []
        if line.get("batch_no"):
            batch_names.append(line["batch_no"])
        if line.get("serial_and_batch_bundle"):
            try:
                bundle = self._document("Serial and Batch Bundle", line["serial_and_batch_bundle"])
                bundles.append(bundle)
                batch_names.extend(row["batch_no"] for row in bundle.get("entries", []) if row.get("batch_no"))
                if bundle.get("entries_truncated"):
                    limits.append("批次包明细已截断。")
            except ERPAccessError as error:
                if error.code == "authorization_revoked":
                    raise
                limits.append(f"序列号/批次包读取受限：{error.code}。")
        batches = []
        for batch_name in list(dict.fromkeys(batch_names))[:self.config.max_lines]:
            try:
                batches.append(self._document("Batch", batch_name))
            except ERPAccessError as error:
                if error.code == "authorization_revoked":
                    raise
                limits.append(f"批次元数据读取受限：{error.code}。")
        if truncated:
            limits.append("预留分页已截断，不能判定完整仓库集合。")
        return self._envelope("Frappe resources: Stock Reservation Entry/Batch/Serial and Batch Bundle", {"delivery_note": delivery_note, "line_name": line_name}, {
            "delivery_note": delivery_note, "line_name": line_name, "line": line,
            "is_return": note.get("is_return"), "delivery_note_modified": note.get("modified"),
            "reservations": reservations, "reservations_truncated": truncated,
            "batches": batches, "bundles": bundles,
            "checked_on": datetime.now(ZoneInfo(self.config.site_timezone)).date().isoformat(),
        }, business_at=f"{note.get('posting_date', '')}T{note.get('posting_time', '')}", limitations=limits)

    def error_events(self, delivery_note: str, occurred_at: datetime) -> dict[str, Any]:
        self._ensure_version()
        self._document("Delivery Note", delivery_note)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=ZoneInfo(self.config.site_timezone))
        moment = occurred_at.astimezone(ZoneInfo(self.config.site_timezone))
        start = (moment - timedelta(seconds=self.config.log_window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        end = (moment + timedelta(seconds=self.config.log_window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        rows, truncated = self._list("Error Log", [
            ["reference_doctype", "=", "Delivery Note"], ["reference_name", "=", delivery_note],
            ["creation", "between", [start, end]],
        ], "creation desc")
        if any(row.get("reference_doctype") != "Delivery Note" or row.get("reference_name") != delivery_note or not start <= str(row.get("creation", "")) <= end for row in rows):
            raise ERPAccessError("error_log_scope_mismatch")
        for row in rows:
            for key in ("error", "method"):
                if key in row and isinstance(row[key], str):
                    row[key] = row[key][:4000]
        return self._envelope("Frappe resource: Error Log", {"delivery_note": delivery_note, "start": start, "end": end, "site_timezone": self.config.site_timezone}, {
            "events": rows, "truncated": truncated,
        }, business_at=occurred_at.isoformat(), limitations=["仅关联发货单且位于时间窗内的 Error Log；没有关联字段的服务器/容器日志尚未接入。"])
