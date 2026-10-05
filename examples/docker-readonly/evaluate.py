"""Added fault evaluation harness; mutations apply only to its freshly created lab.

Diagnosis receives only generated scope config, never the injected fault name.
This is a real Docker/system evaluation of deterministic evidence collection,
not a real-model accuracy benchmark.
"""

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab import ComposeLab, create_lab

from holmes.core.docker_diagnosis import run_diagnosis

SCENARIOS = ("healthy", "database_unavailable", "cache_connection_error", "service_configuration_error")


def write_new_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def safe_version(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=True)
        return result.stdout.strip()[:300]
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def evaluate(output: Path, repeat: int = 1) -> dict[str, Any]:
    if not 1 <= repeat <= 10:
        raise ValueError("repeat must be within 1..10")
    output.mkdir(parents=True, exist_ok=False)
    total_start = time.perf_counter()
    results: list[dict[str, Any]] = []
    for iteration in range(1, repeat + 1):
        for scenario in SCENARIOS:
            config = create_lab()
            lab = ComposeLab(config)
            record: dict[str, Any] = {
                "scenario": scenario, "iteration": iteration, "project": config["project"],
                "passed": False, "cleanup_completed": False,
            }
            setup_start = time.perf_counter()
            try:
                lab.up()
                if scenario != "healthy":
                    lab.fault(scenario)
                record["setup_and_injection_seconds"] = time.perf_counter() - setup_start
                report = run_diagnosis(config)
                name = f"{iteration:02d}-{scenario}.json"
                write_new_json(output / name, report.model_dump(mode="json"))
                codes = [cause.code for cause in report.candidate_causes]
                expected_found = not codes if scenario == "healthy" else scenario in codes
                record.update(
                    report=name, candidate_codes=codes, mode=report.mode,
                    diagnosis_seconds=report.metrics.elapsed_seconds,
                    tool_calls=report.metrics.tool_calls,
                    successful_calls=report.metrics.successful_calls,
                    tool_success_rate=report.metrics.tool_success_rate,
                    expected_candidate_found=expected_found,
                    passed=expected_found and report.metrics.successful_calls == 9,
                )
                if scenario == "database_unavailable":
                    stopped = any(e.observation.get("service") == "mysql" and
                                  e.observation.get("state", {}).get("running") is False
                                  for e in report.supporting_evidence)
                    record["passed"] = record["passed"] and stopped
                if scenario == "cache_connection_error":
                    # Independent probe PING succeeds while api points at the wrong port.
                    redis_ok = any(e.tool_name == "docker_lab_redis" and
                                   e.observation.get("reachable") is True
                                   for e in report.supporting_evidence)
                    record["passed"] = record["passed"] and redis_ok
                if scenario == "service_configuration_error":
                    api_stopped = any(e.observation.get("service") == "api" and
                                      e.observation.get("state", {}).get("running") is False
                                      for e in report.supporting_evidence)
                    record["passed"] = record["passed"] and api_stopped
            except Exception as exc:
                # Raw compose/driver errors can contain sensitive details.
                record["error_type"] = type(exc).__name__
                record["setup_and_injection_seconds"] = time.perf_counter() - setup_start
            finally:
                try:
                    lab.close()
                    record["cleanup_completed"] = True
                except Exception as exc:
                    record["cleanup_error_type"] = type(exc).__name__
                    record["passed"] = False
            results.append(record)
            print(f"{scenario}: {'PASS' if record['passed'] else 'FAIL'}; "
                  f"diagnosis={record.get('diagnosis_seconds', 0):.3f}s; "
                  f"tools={record.get('successful_calls', 0)}/{record.get('tool_calls', 0)}", flush=True)
            # Preserve partial actual outcomes even if a later experiment fails.
            write_new_json(output / f"progress-{len(results):02d}.json", record)
    summary = {
        "evaluation_type": "real-compose-deterministic-evidence; no real LLM calls",
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "platform": platform.platform(),
        "upstream_commit": safe_version(["git", "rev-parse", "HEAD"]),
        "docker": safe_version(["docker", "version", "--format", "{{.Server.Version}}"]),
        "compose": safe_version(["docker", "compose", "version", "--short"]),
        "total_elapsed_seconds": time.perf_counter() - total_start,
        "results": results,
        "passed": all(item["passed"] for item in results),
        "limitations": [
            "每种场景只按 repeat 次执行，不能据此推断长期可靠性。",
            "采证耗时包含工具调用和本地诊断，不包含镜像下载、建容器和故障注入。",
            "本报告未调用真实模型；scripted LLM 的 Agent 接线测试另见 pytest 记录。",
        ],
    }
    write_new_json(output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="运行真实隔离 Compose 故障评测")
    parser.add_argument("--output", type=Path, required=True, help="必须为尚不存在的新目录")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    try:
        summary = evaluate(args.output, args.repeat)
    except Exception as exc:
        print(f"评测未完成 ({type(exc).__name__})。", file=sys.stderr)
        return 1
    print(f"Actual results: {args.output.resolve()}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
