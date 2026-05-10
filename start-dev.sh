#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[dev]${NC} $*"; }
ok()   { echo -e "${GREEN}[ok]${NC}  $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[err]${NC} $*"; }

cleanup() {
  log "正在停止后台服务..."
  kill $API_PID $CLIENT_PID 2>/dev/null || true
  wait $API_PID $CLIENT_PID 2>/dev/null || true
  log "已停止。"
}
trap cleanup EXIT INT TERM

# ── 1. 基础设施 ──────────────────────────────────────────
log "启动 PostgreSQL + Redis（开发容器）..."
docker compose -f docker/docker-compose.dev.yml --env-file .env -p ghostfolio-dev up -d

log "等待 PostgreSQL 就绪..."
until docker exec gf-postgres-dev pg_isready -U chrysantha -d ghostfolio-db 2>/dev/null | grep -q accepting; do
  sleep 1
done
ok "PostgreSQL 就绪"

log "等待 Redis 就绪..."
until docker exec gf-redis-dev redis-cli --pass chrysantha_redis_2026 ping 2>/dev/null | grep -q PONG; do
  sleep 1
done
ok "Redis 就绪"

# ── 2. 环境变量（覆盖为 localhost） ────────────────────────
export DATABASE_URL="postgresql://chrysantha:chrysantha_pg_2026@localhost:5432/ghostfolio-db?connect_timeout=300"
export REDIS_HOST="localhost"
export REDIS_PASSWORD="chrysantha_redis_2026"

# ── 3. 数据库 ──────────────────────────────────────────
log "推送 Prisma schema..."
npx prisma db push
ok "数据库 schema 已同步"

# ── 4. 启动服务 ──────────────────────────────────────────
log "启动 API 服务（后台）..."
npm run start:server &
API_PID=$!

log "启动客户端（HMR，前台）..."
npm run start:client &
CLIENT_PID=$!

log "开发环境已启动！"
log "  API:    http://localhost:3333"
log "  Client: http://localhost:4200"
log ""
log "按 Ctrl+C 停止所有服务..."

wait
