"""Container-local health probe; do not inherit HTTP proxy configuration."""

import json
import sys
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:8000/ready", timeout=4) as response:
        body = json.load(response)
        healthy = response.status == 200 and body.get("dependencies") == {
            "mysql": "ok", "redis": "ok"
        }
except (OSError, ValueError, urllib.error.URLError):
    healthy = False
sys.exit(0 if healthy else 1)
