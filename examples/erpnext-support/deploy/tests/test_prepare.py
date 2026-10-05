"""Build-context privacy and fresh customer runtime tests; no Docker/network."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("deployment_prepare", Path(__file__).parents[1] / "prepare.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


@pytest.fixture
def module(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    directory = repo / "examples/erpnext-support/deploy"
    directory.mkdir(parents=True)
    monkeypatch.setattr(prepare, "ROOT", directory)
    monkeypatch.setattr(prepare, "REPO", repo)
    monkeypatch.setattr(prepare, "restrict_runtime", lambda path: None)
    return prepare


def test_empty_erp_allowlist_and_no_string_frontend(module):
    directory = module.prepare_runtime(port=8766)
    settings = json.loads((directory / "settings.json").read_text())
    assert settings["allowed_erp_origins"] == []
    assert "frontend" not in settings
    assert "http://127.0.0.1:8766" in settings["workbench_origins"]
    initial = json.loads((directory / "initial-login.json").read_text())
    assert len(initial["password"]) >= 24
    assert (directory / "workbench.sqlite3").is_file()


@pytest.mark.parametrize("url", ["http://erp.invalid", "https://user:secret@erp.invalid", "https://erp.invalid/path", "https://erp.invalid?token=value"])
def test_customer_origin_rejected_before_any_runtime_write(module, url):
    with pytest.raises(ValueError):
        module.prepare_runtime(erp_origin=url)
    assert not (module.ROOT / ".runtime").exists()


def test_existing_runtime_is_not_overwritten(module, monkeypatch):
    monkeypatch.setattr(module.secrets, "token_hex", lambda count: "a" * 12)
    directory = module.prepare_runtime()
    initial = (directory / "initial-login.json").read_bytes()
    with pytest.raises(FileExistsError):
        module.prepare_runtime()
    assert (directory / "initial-login.json").read_bytes() == initial


def test_build_allowlist_excludes_runtime_and_reports(module):
    for name in ("pyproject.toml", "poetry.lock", "README.md", "LICENSE"):
        (module.REPO / name).write_text("fixture")
    (module.ROOT / "Dockerfile").write_text("FROM fixture")
    (module.ROOT / ".dockerignore").write_text("**/.runtime")
    (module.REPO / "holmes/.runtime").mkdir(parents=True)
    (module.REPO / "holmes/.runtime/secret.json").write_text("must stay outside context")
    (module.REPO / "holmes/source.py").write_text("pass")
    (module.REPO / "manual-report.json").write_text("must stay outside context")
    frontend = module.REPO / "examples/erpnext-support/frontend"
    (frontend / "src").mkdir(parents=True)
    for name in ("package.json", "package-lock.json", "index.html", "vite.config.js"):
        (frontend / name).write_text("fixture")
    (frontend / "src/main.js").write_text("fixture")
    result = module.prepare_build()
    assert not (result / "holmes/.runtime").exists()
    assert not (result / "manual-report.json").exists()
    manifest = json.loads((result / "build-context-manifest.json").read_text())
    assert "holmes/source.py" in manifest and "frontend/package-lock.json" in manifest


def test_source_junction_rejected(module, monkeypatch):
    source = module.REPO / "linked-source"
    source.mkdir()
    monkeypatch.setattr(Path, "is_junction", lambda path: path == source)
    with pytest.raises(ValueError, match="linked"):
        module.reject_source_links(source)


def test_runtime_traversal_rejected(module):
    with pytest.raises(ValueError, match="stay"):
        module.reject_links(module.ROOT / ".." / ".." / "outside")
