"""Authenticated single-customer support API; only workbench records are mutable."""

import hmac
import asyncio
import json
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from holmes.erpnext_support.engine import diagnose
from holmes.erpnext_support.models import (ActionInput, ApprovalInput, CaseInput, CloseInput,
                                         EnvironmentInput, Login, TicketEdit, TicketInput, UserInput, UserEdit, PasswordChange)
from holmes.erpnext_support.jobs import allow_login, broker_ready, can_access as job_access, dispatch, dispatch_pending, run_task, worker_ready
from holmes.erpnext_support.observability import Metrics, event, request_id, request_id_context
from holmes.erpnext_support.security import password_hash, sanitize, token_hash, verify_password
from holmes.erpnext_support.settings import Settings, origin
from holmes.erpnext_support.storage import AdminInvariant, Store, TaskConflict, UserUnique, utc_now
from holmes.erpnext_support.runtime_tool import RuntimeTool
from holmes.core.docker_diagnosis import EvidenceContextLLM
from holmes.core.tools import ToolInvokeContext
from holmes.plugins.toolsets.erpnext_support import ERPAdapter, ERPConfig


COOKIE = "erp_support_session"


def create_app(settings: Settings, diagnostic: Callable = diagnose, connection_check: Callable | None = None) -> FastAPI:
    store = Store(settings.storage_target)
    metrics = Metrics()
    workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="erp-support")
    login_attempts: dict[str, list[float]] = {}
    login_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.queue_backend == "inline":
            store.recover_interrupted()
        else:
            store.reconcile_expired()
        store.expire_evidence(settings.evidence_retention_days)
        async def maintenance():
            last_expiry = time.monotonic()
            while True:
                await asyncio.sleep(5 if settings.queue_backend == "celery" else 3600)
                try:
                    if settings.queue_backend == "celery":
                        await asyncio.to_thread(dispatch_pending, settings, store)
                    if time.monotonic() - last_expiry >= 3600:
                        await asyncio.to_thread(store.expire_evidence, settings.evidence_retention_days)
                        last_expiry = time.monotonic()
                except Exception as exc:
                    event("maintenance_unavailable", error_type=type(exc).__name__)
        cleanup = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            cleanup.cancel()
            workers.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="ERPNext 库存与发货异常诊断工作台", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.settings = store, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list({"127.0.0.1", "localhost", "testserver", *[urlsplit(v).hostname for v in settings.workbench_origins]}))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default error body includes the supplied password/input.
        fields = [".".join(str(v) for v in error["loc"]) for error in exc.errors()]
        return JSONResponse({"detail": "请求字段无效：" + "、".join(fields), "request_id": request_id_context.get()}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail, "request_id": request_id_context.get()}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(TaskConflict)
    @app.exception_handler(UserUnique)
    @app.exception_handler(AdminInvariant)
    async def conflict_error(request: Request, exc: Exception):
        detail = "该工单已有活动任务或队列已满，请检查后重试。" if isinstance(exc, TaskConflict) else "账号已存在或必须保留有效管理员。"
        if isinstance(exc, TaskConflict) and request.url.path.startswith("/api/environments/"):
            detail = "已有工单引用此环境，不能删除。"
        elif isinstance(exc, TaskConflict) and request.url.path.endswith("/close"):
            detail = "需要最新有效的业务复核；证据不完整时必须明确记录人工例外依据。"
        elif isinstance(exc, TaskConflict) and request.url.path.endswith("/actions"):
            detail = "调查中或已关闭的工单不能记录处理动作。"
        return JSONResponse({"detail": detail, "request_id": request_id_context.get()}, status_code=409)

    async def tracing(request: Request, call_next):
        identifier = request_id(request.headers.get("x-request-id"))
        request.state.request_id = identifier
        token = request_id_context.set(identifier)
        start = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                event("http_error", request_id=identifier, error_type=type(exc).__name__)
                response = JSONResponse({"detail": "服务暂时无法完成请求，请凭请求编号排查。", "request_id": identifier}, status_code=503)
            response.headers["X-Request-ID"] = identifier
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            if request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            route = getattr(request.scope.get("route"), "path", "unmatched")
            elapsed = time.perf_counter() - start
            metrics.observe(request.method, route, response.status_code, elapsed)
            event("http_request", request_id=identifier, method=request.method, route=route, status=response.status_code, elapsed_seconds=round(elapsed, 6))
            if response.status_code in (401, 403) and request.url.path.startswith("/api/"):
                try:
                    store.audit(getattr(request.state, "actor", "anonymous"), "access_denied", route, resource_kind="http", outcome="denied", request_id=identifier)
                except Exception as exc:
                    event("audit_unavailable", request_id=identifier, error_type=type(exc).__name__)
            return response
        finally:
            request_id_context.reset(token)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            request_origin = request.headers.get("origin")
            if request_origin and request_origin not in settings.workbench_origins:
                return JSONResponse({"detail": "请求来源未授权", "request_id": request_id_context.get()}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 65536:
                    return JSONResponse({"detail": "请求超过大小限制", "request_id": request_id_context.get()}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.middleware("http")(tracing)

    def public_user(user: dict) -> dict:
        return {**{k: user[k] for k in ("id", "username", "role", "environment_ids")}, "disabled": bool(user.get("disabled"))}

    def session(request: Request) -> tuple[dict, str]:
        token = request.cookies.get(COOKIE, "")
        if not token:
            raise HTTPException(401, "请先登录工作台")
        row = store.get_session(token_hash(token))
        if not row:
            raise HTTPException(401, "登录已失效，请重新登录")
        user = store.get("user", row["user_id"])
        if not user or user.get("disabled"):
            raise HTTPException(401, "账户不可用")
        request.state.actor = user["username"]
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
        return {"status": "ok", "product": "erpnext-support", "version": "2.0.0", "erp_write_tools": False}

    def system_status() -> dict:
        database_ready = bool(store.readiness().get("ready"))
        queue_ready = broker_ready(settings)
        workers_ready = worker_ready(store) if settings.queue_backend == "celery" else True
        return {"version": "2.0.0", "storage_backend": "postgresql" if settings.database_url else "sqlite",
                "queue_backend": settings.queue_backend, "database_ready": database_ready,
                "broker_ready": queue_ready, "worker_ready": workers_ready}

    @app.get("/ready")
    def readiness(response: Response):
        try:
            state = system_status()
            ready = state["database_ready"] and state["broker_ready"] and state["worker_ready"]
        except Exception:
            state, ready = {"database_ready": False, "broker_ready": False, "worker_ready": False}, False
        response.status_code = 200 if ready else 503
        return {"ready": ready, **state}

    @app.get("/api/system/status")
    def status(current=Depends(session)):
        role(current[0], ("admin",))
        return system_status()

    @app.get("/metrics")
    def prometheus(request: Request):
        supplied = request.headers.get("authorization", "")
        if not settings.metrics_token or not hmac.compare_digest(supplied, "Bearer " + settings.metrics_token):
            raise HTTPException(403, "指标访问需要专用凭据")
        metrics.collect_tasks(store)
        return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/api/session")
    def login(payload: Login, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        if settings.queue_backend == "celery":
            try:
                allowed = allow_login(settings, token_hash(address + "|" + payload.username.casefold()))
            except Exception:
                raise HTTPException(503, "登录安全检查暂时不可用，请稍后重试")
            if not allowed:
                raise HTTPException(429, "登录尝试过多，请稍后重试")
        with login_lock:
            attempts = [v for v in login_attempts.get(address, []) if v > time.monotonic() - 60]
            if len(attempts) >= 15:
                raise HTTPException(429, "登录尝试过多，请稍后重试")
            login_attempts[address] = attempts + [time.monotonic()]
        user = store.find_user(payload.username)
        if not user or user.get("disabled") or not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(401, "账户或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        store.create_session(token_hash(token), user["id"], csrf, (datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)).isoformat())
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=settings.secure_cookies or request.url.scheme == "https", max_age=settings.session_hours * 3600)
        store.audit(user["username"], "login", user["id"])
        return {"user": public_user(user), "csrf_token": csrf}

    @app.get("/api/session")
    def current_session(current=Depends(session)):
        return {"user": public_user(current[0]), "csrf_token": current[1]}

    @app.delete("/api/session")
    def logout(request: Request, response: Response, current=Depends(session)):
        store.delete_session(token_hash(request.cookies.get(COOKIE, "")))
        response.delete_cookie(COOKIE)
        store.audit(current[0]["username"], "logout", current[0]["id"], resource_kind="user")
        return {"ok": True}

    @app.get("/api/users")
    def users(q: str = "", role: str | None = None, disabled: bool | None = None,
              limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), current=Depends(session)):
        filters = {"q": q[:140], **({"role": role} if role else {})}
        if current[0]["role"] != "admin":
            filters["disabled"] = False
        elif disabled is not None:
            filters["disabled"] = disabled
        result = store.page("user", limit, offset, filters=filters)
        return {**result, "items": [public_user(u) for u in result["items"]]}

    @app.post("/api/users")
    def add_user(payload: UserInput, current=Depends(session)):
        role(current[0], ("admin",))
        if store.find_user(payload.username):
            raise HTTPException(409, "账号已存在")
        for identifier in payload.environment_ids:
            authorized(current[0], identifier)
        values = payload.model_dump(exclude={"password"})
        values["password_hash"] = password_hash(payload.password)
        obj = store.create_user_unique(values)
        store.audit(current[0]["username"], "create_user", obj["id"])
        return public_user(obj)

    @app.patch("/api/users/{identifier}")
    def edit_user(identifier: str, payload: UserEdit, current=Depends(session)):
        role(current[0], ("admin",))
        if not store.get("user", identifier):
            raise HTTPException(404, "账号不存在")
        values = payload.model_dump(exclude_unset=True)
        for env_id in values.get("environment_ids", []):
            authorized(current[0], env_id)
        if identifier == current[0]["id"] and (values.get("disabled") is True or values.get("role", "admin") != "admin"):
            raise HTTPException(409, "不能停用或降级当前管理员账号")
        obj = store.update_user_admin_guard(identifier, values)
        store.revoke_user_sessions(identifier)
        store.audit(current[0]["username"], "edit_user", identifier, resource_kind="user", detail={"fields": sorted(values)})
        return public_user(obj)

    @app.post("/api/users/{identifier}/password")
    def reset_password(identifier: str, payload: PasswordChange, current=Depends(session)):
        role(current[0], ("admin",))
        if not store.get("user", identifier):
            raise HTTPException(404, "账号不存在")
        store.update("user", identifier, lambda u: u.update(password_hash=password_hash(payload.password)))
        store.revoke_user_sessions(identifier)
        store.audit(current[0]["username"], "reset_password", identifier, resource_kind="user")
        return {"ok": True}

    @app.get("/api/audit")
    def audit(q: str = "", actor: str | None = None, action: str | None = None, outcome: str | None = None,
              resource_kind: str | None = None, limit: int = Query(50, ge=1, le=100),
              offset: int = Query(0, ge=0, le=100000), current=Depends(session)):
        role(current[0], ("admin",))
        filters = {"q": q[:140], **{k: v[:100] for k, v in {"actor": actor, "action": action, "outcome": outcome, "resource_kind": resource_kind}.items() if v}}
        return store.list_audit(limit, offset, filters=filters)

    @app.put("/api/users/{identifier}/environments")
    def grant_user(identifier: str, payload: UserInput, current=Depends(session)):
        role(current[0], ("admin",))
        if not store.get("user", identifier):
            raise HTTPException(404, "账号不存在")
        for env_id in payload.environment_ids:
            authorized(current[0], env_id)
        if identifier == current[0]["id"] and payload.role != "admin":
            raise HTTPException(409, "不能降级当前管理员账号")
        obj = store.update_user_admin_guard(identifier, {"environment_ids": payload.environment_ids, "role": payload.role})
        store.revoke_user_sessions(identifier)
        store.audit(current[0]["username"], "change_grants", identifier)
        return public_user(obj)

    @app.get("/api/environments")
    def environments(q: str = "", limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), current=Depends(session)):
        user = current[0]
        result = store.page("environment", limit, offset, filters={"q": q[:140]}, environment_ids=None if user["role"] == "admin" else user["environment_ids"])
        return {**result, "items": [view_environment(e) for e in result["items"]]}

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
        store.delete_environment_guarded(identifier)
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
    def tickets(status: str | None = None, environment_id: str | None = None, owner: str | None = None,
                q: str = "", limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), current=Depends(session)):
        user = current[0]
        if environment_id:
            authorized(user, environment_id)
        filters = {"q": q[:140], **{k: v for k, v in {"status": status, "environment_id": environment_id, "owner": owner}.items() if v}}
        return store.page("ticket", limit, offset, filters=filters, environment_ids=None if user["role"] == "admin" else user["environment_ids"])

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
        if obj["status"] in ("closed", "investigating"):
            raise HTTPException(409, "已关闭或调查中的工单不能修改")
        values = payload.model_dump(exclude_unset=True)
        if values.get("description"):
            values["description"] = sanitize(values["description"])
        def apply_edit(ticket: dict):
            if ticket["status"] in ("closed", "investigating"):
                raise HTTPException(409, "当前工单状态不能修改")
            ticket.update(values)
        result = store.update("ticket", identifier, apply_edit)
        store.audit(current[0]["username"], "edit_ticket", identifier)
        return result

    def can_access(user: dict, environment: dict) -> bool:
        return job_access(store, user, environment)

    def schedule(identifier: str, user: dict, kind: str) -> dict:
        role(user, ("admin", "analyst", "reviewer"))
        ticket = ticket_for(user, identifier)
        if ticket["status"] == "closed":
            raise HTTPException(409, "已关闭工单不能重新执行")
        if kind == "review" and not store.list("action", identifier):
            raise HTTPException(409, "请先记录实际人工处理动作，再复核")
        env = authorized(user, ticket["environment_id"])
        credentials(env)
        task = store.create_task(identifier, {"ticket_id": identifier, "environment_id": env["id"], "kind": kind,
            "state": "queued", "steps": [], "result": None, "error": None, "elapsed_seconds": None,
            "requested_by": user["username"], "request_user_id": user["id"], "environment_snapshot": env,
            "ticket_snapshot": ticket, "request_id": request_id_context.get()}, active_limit=settings.task_capacity)
        if settings.queue_backend == "celery":
            dispatch(settings, store, task["id"])
        else:
            workers.submit(run_task, settings, store, task["id"], diagnostic)
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
        obj = store.add_action(identifier, {"actor": current[0]["username"], "description": sanitize(payload.description)})
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
        obj = store.close_ticket(identifier, payload.review_id, sanitize(payload.confirmation), current[0]["username"], payload.manual_override)
        store.audit(current[0]["username"], "manual_close", identifier)
        return obj

    @app.get("/api/cases")
    def cases(q: str = "", status: str | None = None, erpnext_version: str | None = None,
              limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), current=Depends(session)):
        filters = {"q": q[:140], **{k: v for k, v in {"status": status, "erpnext_version": erpnext_version}.items() if v}}
        return store.page("case", limit, offset, filters=filters)

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
        def apply_edit(case: dict):
            # Review may have changed since the initial authorization read.
            if current[0]["role"] == "analyst" and (case["created_by"] != current[0]["username"] or case["status"] != "draft"):
                raise HTTPException(403, "只能编辑本人未审核的案例")
            case.update(**sanitize(payload.model_dump()), status="draft", approved_by=None)
        obj = store.update("case", identifier, apply_edit)
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
