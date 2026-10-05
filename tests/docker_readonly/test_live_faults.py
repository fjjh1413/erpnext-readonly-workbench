"""Opt-in real Docker fault evaluation. Never touches pre-existing resources."""

import importlib
import os
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "docker-readonly"


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("HOLMES_READONLY_LIVE") != "1",
                    reason="Set HOLMES_READONLY_LIVE=1 to create a fresh isolated Docker lab")
def test_real_compose_faults(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    evaluator = importlib.import_module("evaluate")
    destination = Path(os.environ["HOLMES_READONLY_REPORT_DIR"]) if os.environ.get(
        "HOLMES_READONLY_REPORT_DIR") else tmp_path / "actual-compose-results"
    summary = evaluator.evaluate(destination, repeat=1)
    assert len(summary["results"]) == 4
    assert summary["passed"], summary["results"]
    assert all(result["cleanup_completed"] for result in summary["results"])
    assert all(result["tool_calls"] == 9 for result in summary["results"])
