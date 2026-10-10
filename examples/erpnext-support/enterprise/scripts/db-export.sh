#!/bin/sh
set -eu
PGPASSWORD="$(cat /run/secrets/postgres_app_password)"
export PGPASSWORD
exec pg_dump --host=127.0.0.1 --username=workbench_app --dbname=holmes --format=custom --no-owner --no-acl
