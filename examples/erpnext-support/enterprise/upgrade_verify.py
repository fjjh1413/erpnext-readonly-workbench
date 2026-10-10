"""Record a guarded local image update and its preservation checks."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from backup_verify import database_summary, runtime_hashes
from enterprise import Enterprise, checked_path


def run(config: Path, image: str) -> dict:
    started = time.perf_counter()
    source = Enterprise(config)
    previous_config = source.path.read_bytes()
    previous_runtime = runtime_hashes()
    previous = source.resources()
    source.guard(previous)
    stable_services = {item["Config"]["Labels"]["com.docker.compose.service"]: item["Id"]
                       for item in previous["containers"]
                       if item["Config"]["Labels"]["com.docker.compose.service"] not in {"api", "worker", "migrate"}}
    volumes = {item["Name"] for item in previous["volumes"]}
    private_paths = list((source.directory / "secrets").rglob("*"))
    private_paths += [source.directory / "state/settings.json", source.directory / "state/initial-login.json"]
    for name in ("credentials", "collectors"):
        private_paths += list((source.directory / "state" / name).rglob("*"))
    private_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in private_paths if path.is_file()}
    before = database_summary(source)
    result = source.upgrade(image)
    updated = Enterprise(Path(result["config"]))
    resources = updated.resources()
    updated.guard(resources)
    current = {item["Config"]["Labels"]["com.docker.compose.service"]: item for item in resources["containers"]}
    after = database_summary(updated)
    checks = {
        "previous_config_bytes_preserved": source.path.read_bytes() == previous_config,
        "independent_new_version_config": updated.path != source.path and updated.config["image"] == image,
        "database_queue_gateway_monitor_ids_preserved": all(current[name]["Id"] == identity for name, identity in stable_services.items()),
        "all_owned_volume_names_preserved": {item["Name"] for item in resources["volumes"]} == volumes,
        "business_and_audit_record_counts_preserved": before["records"] == after["records"] and before["audit"] == after["audit"],
        "accounts_and_password_hashes_preserved": before["accounts_hash"] == after["accounts_hash"] and before["accounts"] == after["accounts"],
        "existing_sessions_preserved": before["sessions"] == after["sessions"],
        "existing_private_settings_credentials_preserved": all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in private_hashes.items()),
        "other_existing_runtime_files_preserved": runtime_hashes() == previous_runtime,
        "api_and_worker_healthy": all(current[name]["State"].get("Health", {}).get("Status") == "healthy" for name in ("api", "worker")),
        "migration_exit_zero_without_liveness_probe": current["migrate"]["State"]["ExitCode"] == 0 and "Health" not in current["migrate"]["State"],
        "pre_update_private_backup_retained": Path(result["backup"]).is_file(),
    }
    if not all(checks.values()):
        raise AssertionError(next(name for name, passed in checks.items() if not passed))
    return {"layer": "real guarded owned-project image update", "model_called": False,
            "erp_connected": False, "project": updated.config["project"], "config": str(updated.path),
            "previous_config": str(source.path), "previous_image": source.config["image"], "image": image,
            "backup": Path(result["backup"]).name, "contains_sensitive_business_evidence": True,
            "automatic_rollback": False, "checks": checks, "passed": len(checks),
            "records": after["records"], "audit": after["audit"], "accounts": after["accounts"],
            "sessions": after["sessions"], "other_runtime_files_checked": len(previous_runtime),
            "private_files_checked": len(private_hashes), "elapsed_seconds": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = checked_path(args.output)
    if output.exists():
        raise ValueError("A new report path is required before any update")
    result = run(args.config, args.image)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"passed": result["passed"], "config": result["config"], "report": str(output)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
