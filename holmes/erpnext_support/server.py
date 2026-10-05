"""Authenticated single-customer support API; only workbench records are mutable."""

import hmac
import asyncio
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from holmes.erpnext_support.engine import diagnose
from holmes.erpnext_support.models import (ActionInput, ApprovalInput, CaseInput, CloseInput,
                                         EnvironmentInput, Login, TicketEdit, TicketInput, UserInput)
from holmes.erpnext_support.security import password_hash, sanitize, token_hash, verify_password
from holmes.erpnext_support.settings import Settings, origin
from holmes.erpnext_support.storage import Store, utc_now
from holmes.erpnext_support.runtime_tool import RuntimeTool
from holmes.core.docker_diagnosis import EvidenceContextLLM
from holmes.core.tools import ToolInvokeContext
from holmes.plugins.toolsets.erpnext_support import ERPAdapter, ERPConfig


COOKIE = "erp_support_session"


def create_app(settings: Settings, diagnostic: Callable = diagnose, connection_check: Callable | None = None) -> FastAPI:
    store = Store(settings.database)
    workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="erp-support")
    login_attempts: dict[str, list[float]] = {}
    login_lock = threading.Lock()
    task_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.recover_interrupted()
        store.expire_evidence(settings.evidence_retention_days)
        async def maintenance():
            while True:
                await asyncio.sleep(3600)
                await asyncio.to_thread(store.expire_evidence, settings.evidence_retention_days)
        cleanup = asyncio.create_task(maintenance())
        yield
        cleanup.cancel()
        workers.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="ERPNext 库存与发货异常诊断工作台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.settings = store, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list({"127.0.0.1", "localhost", "testserver", *[urlsplit(v).hostname for v in settings.workbench_origins]}))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default error body includes the supplied password/input.
        fields = [".".join(str(v) for v in error["loc"]) for error in exc.errors()]
        return JSONResponse({"detail": "请求字段无效：" + "、".join(fields)}, status_code=422)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            request_origin = request.headers.get("origin")
            if request_origin and request_origin not in settings.workbench_origins:
                return Response('{"detail":"请求来源未授权"}', status_code=403, media_type="application/json")
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 65536:
                    return JSONResponse({"detail": "请求超过大小限制"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def public_user(user: dict) -> dict:
        return {k: user[k] for k in ("id", "username", "role", "environment_ids")}

    def session(request: Request) -> tuple[dict, str]:
        token = request.cookies.get(COOKIE, "")
        if not token:
            raise HTTPException(401, "请先登录工作台")
        with store.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash(token), utc_now())).fetchone()
        if not row:
            raise HTTPException(401, "登录已失效，请重新登录")
        user = store.get("user", row["user_id"])
        if not user or user.get("disabled"):
            raise HTTPException(401, "账户不可用")
        if request.method not in ("GET", "HEAD", "OPTIONS") and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf"]):
            raise HTTPException(403, "请求校验失败，请刷新登录状态")
        return user, row["csrf"]

    def authorized(user: dict, environment_id: str) -> dict:
        environment = store.get("environment", environment_id)
        if not environment:
            raise HTTPException(404, "环境不存在")
        current = store.get("user", user["id"])
        if not current or current.get("disabled") or (current["role"] != "admin" and environment_id not in current["environment_ids"]):
            raise HTTPException(403, "没有此环境的访问权限")
        return environment

    def role(user: dict, roles: tuple[str, ...]) -> None:
        if user["role"] not in roles:
            raise HTTPException(403, "当前角色没有此操作权限")

    def ticket_for(user: dict, identifier: str) -> dict:
        obj = store.get("ticket", identifier)
        if not obj:
            raise HTTPException(404, "工单不存在")
        authorized(user, obj["environment_id"])
        return obj

    def credentials(environment: dict) -> dict:
        try:
            return settings.credentials(environment["credential_ref"])
        except (ValueError, OSError, KeyError):
            raise HTTPException(422, "服务端凭据未配置或无效；请由部署管理员配置，勿在页面粘贴密钥")

    def view_environment(value: dict) -> dict:
        return dict(value)

    @app.get("/health")
    def health():
        return {"status": "ok", "product": "erpnext-support", "erp_write_tools": False}

    @app.post("/api/session")
    def login(payload: Login, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        with login_lock:
            attempts = [v for v in login_attempts.get(address, []) if v > time.monotonic() - 60]
            if len(attempts) >= 15:
                raise HTTPException(429, "登录尝试过多，请稍后重试")
            login_attempts[address] = attempts + [time.monotonic()]
        user = next((u for u in store.list("user") if u["username"] == payload.username), None)
        if not user or user.get("disabled") or not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(401, "账户或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (utc_now(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), user["id"], csrf,
                       (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()))
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=28800)
        store.audit(user["username"], "login", user["id"])
        return {"user": public_user(user), "csrf_token": csrf}

    @app.get("/api/session")
    def current_session(current=Depends(session)):
        return {"user": public_user(current[0]), "csrf_token": current[1]}

    @app.delete("/api/session")
    def logout(request: Request, response: Response, current=Depends(session)):
        with store.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.cookies.get(COOKIE, "")),))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/users")
    def users(current=Depends(session)):
        return {"items": [public_user(u) for u in store.list("user") if not u.get("disabled")]}

    @app.post("/api/users")
    def add_user(payload: UserInput, current=Depends(session)):
        role(current[0], ("admin",))
        if any(u["username"] == payload.username for u in store.list("user")):
            raise HTTPException(409, "账号已存在")
        for identifier in payload.environment_ids:
            authorized(current[0], identifier)
        values = payload.model_dump(exclude={"password"})
        values["password_hash"] = password_hash(payload.password)
        obj = store.add("user", values)
        store.audit(current[0]["username"], "create_user", obj["id"])
        return public_user(obj)

    @app.put("/api/users/{identifier}/environments")
    def grant_user(identifier: str, payload: UserInput, current=Depends(session)):
        role(current[0], ("admin",))
        if not store.get("user", identifier):
            raise HTTPException(404, "账号不存在")
        for env_id in payload.environment_ids:
            authorized(current[0], env_id)
        obj = store.update("user", identifier, lambda u: u.update(environment_ids=payload.environment_ids, role=payload.role))
        store.audit(current[0]["username"], "change_grants", identifier)
        return public_user(obj)

    @app.get("/api/environments")
    def environments(current=Depends(session)):
        user = current[0]
        return {"items": [view_environment(e) for e in store.list("environment") if user["role"] == "admin" or e["id"] in user["environment_ids"]]}

    def validate_environment(payload: EnvironmentInput) -> dict:
        try:
            base = origin(payload.base_url)
        except ValueError:
            raise HTTPException(422, "ERP 地址格式不符合部署策略")
        if base not in settings.allowed_erp_origins:
            raise HTTPException(403, "ERP 地址未在服务端接入白名单中配置")
        if payload.collector_ref:
            try:
                settings.collector_credentials(payload.collector_ref)
            except (ValueError, OSError):
                raise HTTPException(422, "受限采集器凭据别名未在服务端配置")
        elif any(service != "application" for service in payload.allowed_services):
            raise HTTPException(422, "首版仅支持受限应用/API状态；数据库和Redis运行层需接入受限采集器，不能凭API成功宣称正常")
        return {**payload.model_dump(), "base_url": base, "capability_check": None}

    @app.post("/api/environments")
    def add_environment(payload: EnvironmentInput, current=Depends(session)):
        role(current[0], ("admin",))
        obj = store.add("environment", validate_environment(payload))
        store.audit(current[0]["username"], "create_environment", obj["id"])
        return view_environment(obj)

    @app.put("/api/environments/{identifier}")
    def edit_environment(identifier: str, payload: EnvironmentInput, current=Depends(session)):
        role(current[0], ("admin",))
        authorized(current[0], identifier)
        obj = store.update("environment", identifier, lambda e: e.update(validate_environment(payload)))
        store.audit(current[0]["username"], "change_environment", identifier)
        return view_environment(obj)

    @app.delete("/api/environments/{identifier}")
    def delete_environment(identifier: str, current=Depends(session)):
        role(current[0], ("admin",))
        authorized(current[0], identifier)
        if any(t["environment_id"] == identifier for t in store.list("ticket")):
            raise HTTPException(409, "已有工单引用此环境，不能删除")
        store.delete("environment", identifier)
        store.audit(current[0]["username"], "delete_environment", identifier)
        return {"ok": True}

    @app.post("/api/environments/{identifier}/check")
    def check_environment(identifier: str, current=Depends(session)):
        user = current[0]
        env = authorized(user, identifier)
        creds = credentials(env)
        try:
            if connection_check:
                result = connection_check(env, creds)
            else:
                cfg = ERPConfig(base_url=env["base_url"], api_key=creds["api_key"], api_secret=creds["api_secret"],
                                expected_erpnext_version=env["erpnext_version"], expected_frappe_version=env["frappe_version"],
                                timeout_seconds=settings.tool_timeout_seconds, max_rows=settings.max_rows)
                adapter = ERPAdapter(cfg, authorize=lambda: can_access(user, env))
                adapter.begin_tool()
                envelope = adapter.capabilities()
                result = {**envelope.get("payload", {}), "limitations": envelope.get("limitations", []),
                          "source": envelope.get("source"), "collected_at": envelope.get("collected_at")}
            result = sanitize(result, list(creds.values()))
            if env.get("collector_ref"):
                runtime = RuntimeTool(settings.collector_credentials(env["collector_ref"]), env.get("allowed_services", []),
                                      lambda: can_access(user, env), settings.tool_timeout_seconds)
                snapshot = runtime.invoke({}, ToolInvokeContext(llm=EvidenceContextLLM(), max_token_count=8192,
                                          tool_call_id="connection-check", tool_name=runtime.name, tool_number=1))
                result["runtime_collector"] = {"status": snapshot.status.value, "error": snapshot.error,
                                               "snapshot": snapshot.data if snapshot.status.value == "success" else None}
        except Exception as exc:
            result = {"status": "unavailable", "version_supported": False, "missing": [f"接入检查未完成 ({type(exc).__name__})，检查网络、版本和读取权限。"]}
        result = {**result, "checked_at": utc_now()}
        store.update("environment", identifier, lambda e: e.update(capability_check=result))
        store.audit(user["username"], "check_environment", identifier)
        return result

    @app.get("/api/tickets")
    def tickets(status: str | None = None, environment_id: str | None = None, q: str = "", current=Depends(session)):
        user = current[0]
        if environment_id:
            authorized(user, environment_id)
        values = [t for t in store.list("ticket") if (user["role"] == "admin" or t["environment_id"] in user["environment_ids"])
                  and (not status or t["status"] == status) and (not environment_id or t["environment_id"] == environment_id)
                  and (not q or q[:140] in t["delivery_note"] or q[:140] in t["description"])]
        return {"items": values}

    @app.post("/api/tickets")
    def add_ticket(payload: TicketInput, current=Depends(session)):
        user = current[0]
        authorized(user, payload.environment_id)
        role(user, ("admin", "analyst", "reviewer"))
        values = payload.model_dump(mode="json")
        values.update(description=sanitize(payload.description), submitter=user["username"], owner=payload.owner or user["username"], status="pending")
        obj = store.add("ticket", values, environment_id=payload.environment_id)
        store.audit(user["username"], "create_ticket", obj["id"])
        return obj

    @app.get("/api/tickets/{identifier}")
    def ticket_detail(identifier: str, current=Depends(session)):
        ticket_for(current[0], identifier)
        obj = store.ticket_snapshot(identifier)
        if not obj:
            raise HTTPException(404, "工单不存在")
        authorized(current[0], obj["environment_id"])
        return obj

    @app.patch("/api/tickets/{identifier}")
    def edit_ticket(identifier: str, payload: TicketEdit, current=Depends(session)):
        role(current[0], ("admin", "analyst", "reviewer"))
        obj = ticket_for(current[0], identifier)
        if obj["status"] == "closed":
            raise HTTPException(409, "已关闭工单不能修改")
        values = payload.model_dump(exclude_unset=True)
        if values.get("description"):
            values["description"] = sanitize(values["description"])
        result = store.update("ticket", identifier, lambda t: t.update(values))
        store.audit(current[0]["username"], "edit_ticket", identifier)
        return result

    def can_access(user: dict, environment: dict) -> bool:
        current = store.get("user", user["id"])
        live = store.get("environment", environment["id"])
        return bool(current and live and not current.get("disabled") and
                    (current["role"] == "admin" or environment["id"] in current["environment_ids"]) and
                    all(live.get(k) == environment.get(k) for k in ("base_url", "credential_ref", "collector_ref", "allowed_services", "erpnext_version", "frappe_version", "model_consent")))

    def run_task(task: dict, user: dict, env: dict, ticket: dict) -> None:
        start = time.perf_counter()
        store.update("task", task["id"], lambda t: t.update(state="running", started_at=utc_now()))
        try:
            if not can_access(user, env):
                raise PermissionError("Authorization changed")
            creds = credentials(env)
            cases = [c for c in store.list("case") if c["status"] == "approved"]
            result = diagnostic(settings, env, ticket, creds, lambda: can_access(user, env),
                                lambda step: store.update("task", task["id"], lambda t: t["steps"].append(step)), cases)
            if not can_access(user, env):
                raise PermissionError("Authorization revoked")
            if task["kind"] == "review":
                state = result.get("business_state", {})
                blocking = [c for c in result.get("causes", []) if c.get("code") not in ("known_issue_reference",)]
                complete = state.get("business_complete") is True and result.get("evidence_consistent", True) and not blocking
                previous = next((t for t in store.list("task", ticket["id"]) if t["kind"] == "diagnosis" and t.get("result")), None)
                result["review"] = {"business_complete": complete, "requires_human_confirmation": True,
                                    "previous_task_id": previous["id"] if previous else None,
                                    "subtype_checks": [{"code": c["code"], "present_after_handling": any(x["code"] == c["code"] for x in blocking)}
                                                       for c in previous.get("result", {}).get("causes", [])] if previous else [],
                                    "limit": "业务完成必须读取发货单提交状态；接口可用不等于发货完成，状态变化不证明原候选原因正确。"}
            partial = bool(result.get("missing")) or result.get("metrics", {}).get("tool_success_rate", 0) < 1
            next_status = "awaiting_review" if task["kind"] == "review" else "needs_information" if partial else "awaiting_action"
            store.finish_task(task["id"], ticket["id"], {"state": "partial" if partial else "completed", "result": result,
                              "completed_at": utc_now(), "elapsed_seconds": time.perf_counter() - start}, next_status)
        except Exception as exc:
            store.finish_task(task["id"], ticket["id"], {"state": "failed", "completed_at": utc_now(),
                              "elapsed_seconds": time.perf_counter() - start,
                              "error": f"任务未完成 ({type(exc).__name__})；已完成步骤保留，允许重新执行。"}, "needs_information")

    def schedule(identifier: str, user: dict, kind: str) -> dict:
        role(user, ("admin", "analyst", "reviewer"))
        ticket = ticket_for(user, identifier)
        if ticket["status"] == "closed":
            raise HTTPException(409, "已关闭工单不能重新执行")
        if kind == "review" and not store.list("action", identifier):
            raise HTTPException(409, "请先记录实际人工处理动作，再复核")
        with task_lock:
            active = [t for t in store.list("task") if t["state"] in ("queued", "running")]
            if any(t["ticket_id"] == identifier for t in active):
                raise HTTPException(409, "该工单已有执行中的任务")
            if len(active) >= 10:
                raise HTTPException(429, "任务队列已满，请稍后重试")
            env = authorized(user, ticket["environment_id"])
            credentials(env)
            task = store.add("task", {"ticket_id": identifier, "environment_id": env["id"], "kind": kind,
                            "state": "queued", "steps": [], "result": None, "error": None, "elapsed_seconds": None,
                            "requested_by": user["username"]}, environment_id=env["id"], parent_id=identifier)
            store.update("ticket", identifier, lambda t: t.update(status="investigating"))
            workers.submit(run_task, task, user, env, ticket)
        store.audit(user["username"], "start_" + kind, task["id"])
        return {"task": task}

    @app.post("/api/tickets/{identifier}/diagnose", status_code=202)
    def start_diagnosis(identifier: str, current=Depends(session)):
        return schedule(identifier, current[0], "diagnosis")

    @app.post("/api/tickets/{identifier}/actions")
    def record_action(identifier: str, payload: ActionInput, current=Depends(session)):
        role(current[0], ("admin", "analyst", "reviewer"))
        ticket = ticket_for(current[0], identifier)
        if ticket["status"] in ("closed", "investigating"):
            raise HTTPException(409, "当前工单状态不能记录处理动作")
        obj = store.add("action", {"actor": current[0]["username"], "description": sanitize(payload.description)},
                        environment_id=ticket["environment_id"], parent_id=identifier)
        store.update("ticket", identifier, lambda t: t.update(status="awaiting_review"))
        store.audit(current[0]["username"], "record_human_action", identifier)
        return obj

    @app.post("/api/tickets/{identifier}/review", status_code=202)
    def start_review(identifier: str, current=Depends(session)):
        return schedule(identifier, current[0], "review")

    @app.post("/api/tickets/{identifier}/close")
    def close_ticket(identifier: str, payload: CloseInput, current=Depends(session)):
        role(current[0], ("admin", "reviewer"))
        ticket = ticket_for(current[0], identifier)
        if ticket["status"] != "awaiting_review":
            raise HTTPException(409, "工单必须处于待复核阶段且没有执行中的任务")
        latest = next((t for t in store.list("task", identifier) if t["kind"] == "review"), None)
        if not latest or latest["id"] != payload.review_id or latest["state"] not in ("completed", "partial"):
            raise HTTPException(409, "需要本工单最新已完成的复核记录")
        latest_action = next(iter(store.list("action", identifier)), None)
        if not latest_action or latest_action["created_at"] > latest["created_at"]:
            raise HTTPException(409, "人工处理记录发生变化，必须重新读取ERP进行复核")
        verified = latest.get("result", {}).get("review", {}).get("business_complete", False)
        if not verified and (not payload.manual_override or len(payload.confirmation) < 20):
            raise HTTPException(409, "业务尚未完整通过复核；人工例外关闭须显式确认并记录至少20字的依据")
        obj = store.update("ticket", identifier, lambda t: t.update(status="closed", closed_by=current[0]["username"],
                           closed_at=utc_now(), close_confirmation=sanitize(payload.confirmation),
                           review_id=payload.review_id, manual_override=payload.manual_override))
        store.audit(current[0]["username"], "manual_close", identifier)
        return obj

    @app.get("/api/cases")
    def cases(q: str = "", current=Depends(session)):
        return {"items": [c for c in store.list("case") if not q or q[:140] in c["title"] or q[:140] in c["symptom"]]}

    @app.post("/api/cases")
    def add_case(payload: CaseInput, current=Depends(session)):
        role(current[0], ("admin", "analyst", "reviewer"))
        obj = store.add("case", {**sanitize(payload.model_dump()), "status": "draft", "created_by": current[0]["username"]})
        store.audit(current[0]["username"], "draft_case", obj["id"])
        return obj

    @app.put("/api/cases/{identifier}")
    def edit_case(identifier: str, payload: CaseInput, current=Depends(session)):
        role(current[0], ("admin", "analyst", "reviewer"))
        existing = store.get("case", identifier)
        if not existing:
            raise HTTPException(404, "案例不存在")
        if current[0]["role"] == "analyst" and (existing["created_by"] != current[0]["username"] or existing["status"] != "draft"):
            raise HTTPException(403, "只能编辑本人未审核的案例")
        obj = store.update("case", identifier, lambda c: c.update(**sanitize(payload.model_dump()), status="draft", approved_by=None))
        store.audit(current[0]["username"], "edit_case_revoke_approval", identifier)
        return obj

    @app.post("/api/cases/{identifier}/approve")
    def approve_case(identifier: str, payload: ApprovalInput, current=Depends(session)):
        role(current[0], ("admin", "reviewer"))
        if not store.get("case", identifier):
            raise HTTPException(404, "案例不存在")
        obj = store.update("case", identifier, lambda c: c.update(status=payload.decision, approved_by=current[0]["username"],
                           approval_note=sanitize(payload.note), approved_at=utc_now()))
        store.audit(current[0]["username"], "review_case", identifier)
        return obj

    @app.delete("/api/cases/{identifier}")
    def delete_case(identifier: str, current=Depends(session)):
        role(current[0], ("admin", "reviewer"))
        store.delete("case", identifier)
        store.audit(current[0]["username"], "delete_case", identifier)
        return {"ok": True}

    @app.get("/{asset_path:path}", include_in_schema=False)
    def frontend(asset_path: str):
        if asset_path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        root = settings.frontend.resolve()
        candidate = (root / asset_path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(403, "路径不允许")
        if candidate.is_file():
            return FileResponse(candidate)
        index = root / "index.html"
        if not index.is_file():
            raise HTTPException(503, "前端尚未构建，请按README执行npm ci和npm run build")
        return FileResponse(index)

    return app
