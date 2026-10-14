#!/bin/bash
# 在华为云上安装。不改 80/443（面试站点的 Caddy 仍反代到 172.28.0.1:18900）。
# Python 在镜像里，不放在 /root，也不在宿主机另装一份解释器。
set -euo pipefail
cd /srv/teacher-platform
if ! systemctl cat docker.service >/dev/null 2>&1; then
  echo "找不到 docker.service，无法把解释器放进容器。" >&2
  exit 1
fi
mkdir -p data
if [ ! -f .env ]; then
  umask 077
  pass="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  cat > .env << EOF
BIND_HOST=172.28.0.1
BIND_PORT=18900
DATA_DIR=/srv/teacher-platform/data
ADMIN_USERNAME=teacher
ADMIN_PASSWORD=${pass}
ADMIN_DISPLAY_NAME=老师
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
CORPUS_ROOT=/data/corpus
# 公网提供 HTTPS 后再改成 1。现在前面是 HTTP 时保持 0，否则浏览器不会带上登录 Cookie。
COOKIE_SECURE=0
EOF
  printf '%s\n' "$pass" > initial-password.txt
  chmod 600 .env initial-password.txt
fi

echo "构建镜像（现网进程先继续服务）"
docker compose -f deploy/docker-compose.yml build

cp -a /etc/systemd/system/teacher-platform.service /etc/systemd/system/teacher-platform.service.hostbak
systemctl stop teacher-platform
chown -R 10001:10001 data

if ! docker compose -f deploy/docker-compose.yml up -d --wait; then
  echo "容器没起来，退回宿主机上的旧进程。" >&2
  docker compose -f deploy/docker-compose.yml down
  systemctl start teacher-platform
  exit 1
fi
if ! curl -fsS http://172.28.0.1:18900/healthz; then
  echo "健康检查失败，退回宿主机上的旧进程。" >&2
  docker compose -f deploy/docker-compose.yml down
  systemctl start teacher-platform
  exit 1
fi
echo
ss -lntp | awk '/18900/'
cp deploy/teacher-platform.service /etc/systemd/system/teacher-platform.service
systemctl daemon-reload
systemctl enable teacher-platform
systemctl start teacher-platform
rm -rf /opt/python
echo "教师助手已在容器里运行。"
