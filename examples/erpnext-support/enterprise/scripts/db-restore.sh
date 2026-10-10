#!/bin/sh
set -eu
PGPASSWORD="$(cat /run/secrets/postgres_migration_password)"
export PGPASSWORD
exec pg_restore --host=127.0.0.1 --username=workbench_migrator --dbname=holmes --no-owner --no-acl --exit-on-error --single-transaction
