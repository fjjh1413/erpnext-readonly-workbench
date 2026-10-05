#!/bin/sh
set -eu
# REDISCLI_AUTH is scoped to this short-lived check and never appears in argv.
REDISCLI_AUTH="$(cat /run/secrets/redis_probe_password)" \
    redis-cli --user probe --no-auth-warning ping 2>/dev/null | grep -qx PONG
