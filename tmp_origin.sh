#!/bin/bash
echo "=== via caddy ==="
curl -sS -D - -o /tmp/logout-caddy.txt -X POST \
  -H "Host: 117.78.6.106" \
  -H "Origin: http://117.78.6.106" \
  http://127.0.0.1/logout
echo
echo "body: $(cat /tmp/logout-caddy.txt)"
echo "=== direct app ==="
curl -sS -D - -o /tmp/logout-app.txt -X POST \
  -H "Host: 117.78.6.106" \
  -H "Origin: http://117.78.6.106" \
  http://172.28.0.1:18900/logout
echo
echo "body: $(cat /tmp/logout-app.txt)"
echo "=== caddy site ==="
python3 - << 'PY'
from pathlib import Path
text = Path("/opt/agent-interview-server/Caddyfile").read_text(encoding="utf-8", errors="replace")
start = text.find("teacher-platform")
print(text[max(0, start-200):start+800] if start >= 0 else "NOT_FOUND")
PY
