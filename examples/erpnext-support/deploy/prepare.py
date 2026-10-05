"""Prepare fresh build/runtime directories without copying private workspace data."""

import argparse
import hashlib
import json
import secrets
import shutil
from pathlib import Path

from holmes.erpnext_support.security import password_hash
from holmes.erpnext_support.settings import Settings, origin, restrict_runtime
from holmes.erpnext_support.storage import Store

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]


def reject_links(path):
    path = Path(path).absolute()
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Deployment output must stay in this module")
    for current in (path, *path.parents):
        if current.is_symlink() or current.is_junction():
            raise ValueError("Links and junctions cannot be used as deployment output")
        if current == ROOT:
            break


def reject_source_links(path):
    if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(REPO.resolve()):
        raise ValueError("Build sources cannot contain linked or external workspace content")
    if path.is_dir():
        for child in path.iterdir():
            if child.name not in {"__pycache__", ".runtime", "node_modules", ".env", "credentials", "collectors"}:
                reject_source_links(child)


def prepare_build():
    destination = ROOT / ".runtime" / ("build-" + secrets.token_hex(6))
    reject_links(destination)
    destination.mkdir(parents=True, exist_ok=False)
    frontend_source = REPO / "examples" / "erpnext-support" / "frontend"
    for source in (REPO / "holmes", frontend_source / "src",
                   *(frontend_source / name for name in ("package.json", "package-lock.json", "index.html", "vite.config.js")),
                   *(REPO / name for name in ("pyproject.toml", "poetry.lock", "README.md", "LICENSE"))):
        reject_source_links(source)
    # Deliberate allowlist: never copy the repo root, runtime, reports or .git.
    for name in ("pyproject.toml", "poetry.lock", "README.md", "LICENSE"):
        shutil.copyfile(REPO / name, destination / name)
    shutil.copyfile(ROOT / "Dockerfile", destination / "Dockerfile")
    shutil.copyfile(ROOT / ".dockerignore", destination / ".dockerignore")
    shutil.copytree(REPO / "holmes", destination / "holmes",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".runtime", ".env", "credentials", "collectors"))
    frontend = destination / "frontend"
    frontend.mkdir()
    source = REPO / "examples" / "erpnext-support" / "frontend"
    for name in ("package.json", "package-lock.json", "index.html", "vite.config.js"):
        shutil.copyfile(source / name, frontend / name)
    shutil.copytree(source / "src", frontend / "src")
    inventory = {path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in destination.rglob("*") if path.is_file()}
    with (destination / "build-context-manifest.json").open("x") as stream:
        json.dump(inventory, stream, indent=2)
    return destination


def prepare_runtime(erp_origin=None, port=8765):
    if erp_origin and not erp_origin.startswith("https://"):
        raise ValueError("Customer ERP allowlist requires an explicit HTTPS origin")
    origins = [origin(erp_origin)] if erp_origin else []
    directory = ROOT / ".runtime" / ("instance-" + secrets.token_hex(6))
    reject_links(directory)
    directory.mkdir(parents=True, exist_ok=False)
    restrict_runtime(directory)
    (directory / "credentials").mkdir()
    values = {"allowed_erp_origins": origins,
              "workbench_origins": [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]}
    with (directory / "settings.json").open("x", encoding="utf-8") as stream:
        json.dump(values, stream, indent=2)
    password = secrets.token_urlsafe(24)
    with (directory / "initial-login.json").open("x", encoding="utf-8") as stream:
        json.dump({"username": "admin", "password": password}, stream)
    settings = Settings.load(directory / "settings.json")
    store = Store(settings.database)
    store.add("user", {"username": "admin", "password_hash": password_hash(password),
                       "role": "admin", "environment_ids": []})
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build-context", "runtime"))
    parser.add_argument("--erp-origin")
    parser.add_argument("--port", type=int, choices=(8765, 8766), default=8765)
    args = parser.parse_args()
    print(prepare_build() if args.action == "build-context" else prepare_runtime(args.erp_origin, args.port))
