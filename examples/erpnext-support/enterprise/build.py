"""Explicit source allowlist for a fresh enterprise image build context."""

import hashlib
import json
import secrets
import shutil
from pathlib import Path

from enterprise import ROOT, checked_path


REPO = ROOT.parents[2]
IGNORE = {"__pycache__", ".runtime", "node_modules", ".env", "credentials", "collectors"}


def reject_source(path: Path):
    if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(REPO.resolve()):
        raise ValueError("Build source is linked or outside the workspace")
    if path.is_dir():
        for child in path.iterdir():
            if child.name not in IGNORE and child.suffix != ".pyc":
                reject_source(child)


def prepare():
    frontend = REPO / "examples" / "erpnext-support" / "frontend"
    frontend_names = ["package.json", "package-lock.json", "index.html"]
    frontend_names += [p.name for p in frontend.glob("vite.config.*") if p.suffix in {".js", ".ts"}]
    frontend_names += [p.name for p in frontend.glob("tsconfig*.json")]
    if not any(name.startswith("vite.config.") for name in frontend_names):
        raise ValueError("Frontend build configuration is missing")
    sources = [REPO / "holmes", frontend / "src", ROOT / "requirements.txt", ROOT / "health.py", ROOT / "Dockerfile", ROOT / ".dockerignore",
               *[frontend / name for name in frontend_names],
               *[REPO / name for name in ("pyproject.toml", "poetry.lock", "README.md", "LICENSE")]]
    for source in sources:
        reject_source(source)
    destination = checked_path(ROOT / ".runtime" / ("build-" + secrets.token_hex(6)))
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("pyproject.toml", "poetry.lock", "README.md", "LICENSE"):
        shutil.copyfile(REPO / name, destination / name)
    for name in ("Dockerfile", ".dockerignore"):
        shutil.copyfile(ROOT / name, destination / name)
    shutil.copytree(REPO / "holmes", destination / "holmes", ignore=shutil.ignore_patterns(*IGNORE, "*.pyc"))
    (destination / "enterprise").mkdir()
    for name in ("requirements.txt", "health.py"):
        shutil.copyfile(ROOT / name, destination / "enterprise" / name)
    (destination / "frontend").mkdir()
    for name in frontend_names:
        shutil.copyfile(frontend / name, destination / "frontend" / name)
    shutil.copytree(frontend / "src", destination / "frontend" / "src")
    manifest = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in destination.rglob("*") if p.is_file()}
    with (destination / "build-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
    return destination


if __name__ == "__main__":
    print(prepare())
