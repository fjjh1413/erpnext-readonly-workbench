"""Explicit local deployment and workbench backup commands."""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import uvicorn

from holmes.erpnext_support.security import password_hash, safe_path
from holmes.erpnext_support.server import create_app
from holmes.erpnext_support.persistence.migrate import grant_application, initialize_database
from holmes.erpnext_support.observability import configure_logging
from holmes.erpnext_support.settings import EXAMPLE, Settings, initialize, restrict_runtime
from holmes.erpnext_support.storage import Store, UserUnique


def ensure_initial_account(settings: Settings) -> None:
    store = Store(settings.storage_target)
    try:
        if store.list("user"):
            return
        path = safe_path(settings.runtime, settings.runtime / "initial-login.json")
        if path.stat().st_size > 4096:
            raise ValueError("初始化账户文件无效")
        initial = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(initial.get("password"), str) or len(initial["password"]) < 12:
            raise ValueError("初始化密码长度不足")
        try:
            store.create_user_unique({"username": initial["username"], "password_hash": password_hash(initial["password"]),
                                      "role": "admin", "environment_ids": [], "disabled": False})
        except UserUnique:
            # Another API instance may have completed the same bootstrap.
            if not store.find_user(initial["username"]):
                raise
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="ERPNext库存与发货异常诊断工作台")
    parser.add_argument("action", choices=("init", "serve", "check", "backup", "restore", "migrate"))
    parser.add_argument("--settings", type=Path, default=Path(os.environ.get("ERP_SUPPORT_SETTINGS", EXAMPLE / ".runtime/workbench/settings.json")))
    parser.add_argument("--erp-url")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            if not args.erp_url:
                parser.error("init需要--erp-url，绑定已授权ERP origin")
            created = initialize(args.settings.parent, args.erp_url)
            settings = Settings.load(created)
            ensure_initial_account(settings)
            print(f"已初始化：{created}；登录凭据在同目录 initial-login.json（仅本机受限文件，不输出密码）。")
            return 0
        if args.action == "restore":
            if not args.backup or not args.output:
                parser.error("restore需要--backup和全新--output文件；不会覆盖运行中的数据库")
            if args.output.exists():
                raise FileExistsError("拒绝覆盖现有数据库")
            with sqlite3.connect(f"file:{args.backup.resolve().as_posix()}?mode=ro", uri=True) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份完整性验证失败")
                with sqlite3.connect(args.output) as target:
                    source.backup(target)
            restrict_runtime(args.output)
            print(f"工作台数据库恢复到全新文件：{args.output.resolve()}；凭据需要独立恢复。")
            return 0
        settings = Settings.load(args.settings)
        if args.action == "migrate":
            migration_file = os.environ.get("ERP_SUPPORT_MIGRATION_DATABASE_URL_FILE")
            if not migration_file:
                raise ValueError("迁移必须单独提供数据库迁移账号配置文件")
            migration_path = Path(migration_file).absolute()
            safe_path(migration_path.parent, migration_path)
            if migration_path.stat().st_size > 4096:
                raise ValueError("迁移配置文件无效")
            migration_url = migration_path.read_text(encoding="utf-8-sig").strip()
            initialize_database(migration_url)
            grant_application(migration_url)
            print(json.dumps({"database_migrated": True, "application_role": "workbench_app"}))
            return 0
        if not settings.database_url and not settings.database.exists():
            raise FileNotFoundError("请先运行init")
        store = Store(settings.storage_target)
        if args.action == "check":
            state = store.readiness()
            if not state.get("ready"):
                raise ValueError("数据库尚未就绪")
            print(json.dumps({"database": "ok", "storage_backend": "postgresql" if settings.database_url else "sqlite", "queue_backend": settings.queue_backend,
                              "allowed_erp_origins": settings.allowed_erp_origins,
                              "model_configured": bool(settings.model and settings.model_api_key),
                              "model_target": settings.model_api_base, "credentials": [p.stem for p in (settings.runtime / "credentials").glob("*.json")]}, ensure_ascii=False))
        elif args.action == "backup":
            if not args.output:
                parser.error("backup需要全新--output文件")
            fresh_parent = not args.output.parent.exists()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if fresh_parent:
                restrict_runtime(args.output.parent)
            store.backup(args.output)
            restrict_runtime(args.output)
            print(f"工作台数据库已备份：{args.output.resolve()}；不包含ERP数据库或API凭据。")
        else:
            if not 1024 <= args.port <= 65535:
                raise ValueError("端口不在允许范围")
            ensure_initial_account(settings)
            configure_logging()
            with (settings.runtime / ("server-" + uuid.uuid4().hex + ".json")).open("x", encoding="utf-8") as manifest:
                json.dump({"pid": os.getpid(), "module": "holmes.erpnext_support", "settings": str(args.settings.resolve())}, manifest)
            uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
        return 0
    except Exception as exc:
        # Driver exceptions can contain connection strings and SQL parameters.
        print(f"操作未完成 ({type(exc).__name__})，请检查明确配置的路径和权限。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
