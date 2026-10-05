#!/bin/sh
set -eu
password="$(cat /run/secrets/redis_probe_password)"
test "${#password}" = 64
case "$password" in *[!a-f0-9]*) exit 1 ;; esac
password_hash="$(printf %s "$password" | sha256sum | cut -d' ' -f1)"
umask 022
# redis-py may issue CLIENT SETINFO. The ACL intentionally denies it, as well
# as GET/SET/CONFIG; the readiness client uses only AUTH and PING.
printf 'user default off\nuser probe on #%s -@all +ping\n' "$password_hash" > /tmp/probe.acl
unset password password_hash
exec /usr/local/bin/docker-entrypoint.sh redis-server \
    --aclfile /tmp/probe.acl --save '' --appendonly no --protected-mode yes
