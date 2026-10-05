#!/bin/bash
set -eu
client_file="$(mktemp /tmp/holmes-mysql-health.XXXXXX)"
trap 'rm -f "$client_file"' EXIT
chmod 600 "$client_file"
printf '[client]\nuser=probe\npassword=%s\nprotocol=tcp\nhost=127.0.0.1\n' "$(cat /run/secrets/mysql_probe_password)" > "$client_file"
test "$(mysql --defaults-extra-file="$client_file" --batch --skip-column-names --execute='SELECT 1' 2>/dev/null)" = 1
