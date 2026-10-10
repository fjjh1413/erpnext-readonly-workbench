"""Hermetic image-context scope checks; no Docker or secret reads."""

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def build_fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    root = repo / "examples/erpnext-support/enterprise"
    frontend = root.parent / "frontend"
    files = {"holmes/app.py": "print('app')", "holmes/.runtime/private.json": "private-fixture",
             "holmes/__pycache__/private.pyc": "private-fixture",
             "examples/erpnext-support/frontend/src/app.ts": "export const version = 2",
             "examples/erpnext-support/frontend/package.json": "{}",
             "examples/erpnext-support/frontend/package-lock.json": "{}",
             "examples/erpnext-support/frontend/index.html": "<html></html>",
             "examples/erpnext-support/frontend/vite.config.ts": "export default {}",
             "examples/erpnext-support/frontend/tsconfig.json": "{}",
             "examples/erpnext-support/frontend/node_modules/private.txt": "private-fixture",
             "examples/erpnext-support/enterprise/requirements.txt": "dependency==1.0",
             "examples/erpnext-support/enterprise/health.py": "pass",
             "examples/erpnext-support/enterprise/Dockerfile": "FROM reviewed-image",
             "examples/erpnext-support/enterprise/.dockerignore": ".runtime",
             "examples/erpnext-support/enterprise/.runtime/private.json": "private-fixture",
             "pyproject.toml": "[tool.poetry]", "poetry.lock": "frozen-lock",
             "README.md": "upstream", "LICENSE": "upstream-license"}
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    stub = types.ModuleType("enterprise")
    stub.ROOT = root
    stub.checked_path = lambda path: path
    monkeypatch.setitem(sys.modules, "enterprise", stub)
    spec = importlib.util.spec_from_file_location("enterprise_build_test", SOURCE / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, repo, root, frontend


@pytest.mark.parametrize("name", ["Dockerfile", ".dockerignore"])
@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_build_definitions_reject_links_before_context_creation(build_fixture, monkeypatch, name, kind):
    build, _, root, _ = build_fixture
    original = getattr(Path, kind)
    monkeypatch.setattr(Path, kind, lambda path: path == root / name or original(path))
    before = set((root / ".runtime").iterdir())
    with pytest.raises(ValueError, match="linked"):
        build.prepare()
    assert set((root / ".runtime").iterdir()) == before


def test_context_preserves_lock_typescript_and_licenses_without_private_inputs(build_fixture):
    build, repo, _, _ = build_fixture
    before = (repo / "poetry.lock").read_bytes()
    context = build.prepare()
    manifest = json.loads((context / "build-manifest.json").read_text(encoding="utf-8"))
    assert (context / "poetry.lock").read_bytes() == before == (repo / "poetry.lock").read_bytes()
    assert all(name in manifest for name in ("frontend/vite.config.ts", "frontend/tsconfig.json", "LICENSE", "Dockerfile", ".dockerignore"))
    assert not any(part in name.split("/") for name in manifest for part in (".runtime", "__pycache__", "node_modules"))
    assert all(hashlib.sha256((context / name).read_bytes()).hexdigest() == digest for name, digest in manifest.items())
