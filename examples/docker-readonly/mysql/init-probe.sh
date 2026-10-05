#!/bin/bash
set -eu
# The bootstrap account is used only inside this newly created lab container.
# probe receives no table privilege; its diagnostic statement is SELECT 1.
probe_password="$(cat /run/secrets/mysql_probe_password)"
root_password="$(cat /run/secrets/mysql_root_password)"
[[ "$probe_password" =~ ^[a-f0-9]{64}$ ]] || exit 1
[[ "$root_password" =~ ^[a-f0-9]{64}$ ]] || exit 1
client_file="$(mktemp /tmp/holmes-mysql-init.XXXXXX)"
trap 'rm -f "$client_file"' EXIT
chmod 600 "$client_file"
printf '[client]\nuser=root\npassword=%s\nprotocol=socket\n' "$root_password" > "$client_file"
mysql --defaults-extra-file="$client_file" <<SQL
CREATE USER 'probe'@'%' IDENTIFIED BY '$probe_password';
GRANT USAGE ON *.* TO 'probe'@'%';
SQL
rm -f "$client_file"
trap - EXIT
