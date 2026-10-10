"""Owned backup/restore rehearsal; target is always newly created and removed."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from enterprise import Enterprise, ROOT, checked_path, create


def runtime_hashes():
    repo = ROOT.parents[2]
    paths = [repo / "examples/docker-readonly/.runtime", repo / "examples/erpnext-support/.runtime",
             repo / "examples/erpnext-support/lab/.runtime", repo / "examples/erpnext-support/deploy/.runtime"]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for directory in paths if directory.exists()
            for path in directory.rglob("*") if path.is_file() and not path.is_symlink() and not path.is_junction()}


def database_summary(lab):
    resources = lab.resources()
    lab.guard(resources)
    db = next(x for x in resources["containers"] if x["Config"]["Labels"]["com.docker.compose.service"] == "db")
    # Only aggregate checks and a hash of account records leave this container.
    query = ("SELECT json_build_object('records',(SELECT count(*) FROM records WHERE kind <> 'worker'),"
             "'audit',(SELECT count(*) FROM audit_events),'sessions',(SELECT count(*) FROM sessions),"
             "'accounts_hash',(SELECT md5(COALESCE(string_agg(payload::text,'' ORDER BY id),'')) FROM records WHERE kind='user'),"
             "'accounts',(SELECT count(*) FROM records WHERE kind='user'))")
    command = ('PGPASSWORD="$(cat /run/secrets/postgres_migration_password)"; export PGPASSWORD; '
               'psql --host=127.0.0.1 --username=workbench_migrator --dbname=holmes --no-psqlrc '
               '--set=ON_ERROR_STOP=1 -At -c "' + query + '"')
    return json.loads(lab.docker(["exec", "--user", "999:999", db["Id"], "sh", "-c", command]))


def run(config):
    started = time.perf_counter()
    source = Enterprise(config)
    old_runtime = runtime_hashes()
    source_files = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name in ("secrets", "state") for path in (source.directory / name).rglob("*") if path.is_file()}
    initial = database_summary(source)
    backup = source.backup()
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    target_config = create(image=source.config["image"])
    target = Enterprise(target_config)
    try:
        target.up(data_only=True)
        restored = target.restore(backup)
        actual = database_summary(target)
        unchanged = database_summary(source)
        checks = {
            "fresh_target_only": restored["restored_into_empty_database"],
            "source_backup_unchanged": hashlib.sha256(backup.read_bytes()).hexdigest() == digest,
            "business_record_count_preserved": initial["records"] == actual["records"],
            "audit_record_count_preserved": initial["audit"] == actual["audit"],
            "account_records_and_password_hashes_preserved": initial["accounts_hash"] == actual["accounts_hash"] and initial["accounts"] == actual["accounts"],
            "restored_sessions_revoked": actual["sessions"] == 0 and restored["sessions_revoked"],
            "source_sessions_preserved": initial["sessions"] == unchanged["sessions"],
            "other_existing_runtime_files_unchanged": runtime_hashes() == old_runtime,
            "source_private_files_unchanged": all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in source_files.items()),
        }
        if not all(checks.values()):
            raise AssertionError(next(key for key, value in checks.items() if not value))
        return {"layer": "real PostgreSQL pg_dump/fresh empty restore", "model_called": False,
                "source_project": source.config["project"], "target_project": target.config["project"],
                "checks": checks, "passed": len(checks), "records": actual["records"], "audit": actual["audit"],
                "accounts": actual["accounts"], "source_sessions": initial["sessions"], "restored_sessions": actual["sessions"],
                "other_runtime_files_checked": len(old_runtime), "contains_erp_database": False,
                "contains_sensitive_business_evidence": True, "sessions_revoked": True,
                "elapsed_seconds": time.perf_counter() - started}
    finally:
        # Only the newly created rehearsal target is removed; source stays live.
        target.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = checked_path(args.output)
    if output.exists():
        raise ValueError("A new output path is required")
    result = run(args.config)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"passed": result["passed"], "report": str(output)}))


if __name__ == "__main__":
    main()
