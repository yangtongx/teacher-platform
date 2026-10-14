#!/bin/bash
set -euo pipefail
# 停掉上次用官方 PyPI 的构建。按脚本名匹配，避免误伤本脚本。
if [ -f /tmp/teacher-docker-install.pid ]; then
  kill "$(cat /tmp/teacher-docker-install.pid)" 2>/dev/null || true
fi
pkill -f "teacher-docker-install.log" 2>/dev/null || true
docker ps -q --filter "ancestor=python:3.12-slim-bookworm" | xargs -r docker kill >/dev/null 2>&1 || true
sleep 1

rm -rf /tmp/teacher-platform-src
git clone --depth 1 https://github.com/yangtongx/teacher-platform.git /tmp/teacher-platform-src

rsync -a \
  --exclude data \
  --exclude .env \
  --exclude .venv \
  --exclude .venv312 \
  --exclude initial-password.txt \
  /tmp/teacher-platform-src/ /srv/teacher-platform/

cp /tmp/Dockerfile.mirror /srv/teacher-platform/deploy/Dockerfile
sed -i 's/\r$//' /srv/teacher-platform/deploy/Dockerfile /srv/teacher-platform/deploy/install.sh /srv/teacher-platform/deploy/docker-compose.yml

cd /srv/teacher-platform
nohup bash deploy/install.sh > /tmp/teacher-docker-install.log 2>&1 &
echo $! > /tmp/teacher-docker-install.pid
echo STARTED
sleep 2
head -n 30 /tmp/teacher-docker-install.log
