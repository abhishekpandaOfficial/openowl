#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# OpenOwl Orchestrator Script
# Run: chmod +x scripts/start.sh && ./scripts/start.sh
# Commands: start | status | stop | restart | logs
# ═══════════════════════════════════════════════════════════

set -uo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RUNTIME_DIR="$ROOT_DIR/.openowl-runtime"
LOG_DIR="$RUNTIME_DIR/logs"
PID_FILE="$RUNTIME_DIR/ngrok.pid"
NGROK_LOG="$LOG_DIR/ngrok.log"

mkdir -p "$LOG_DIR"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    COMPOSE=()
fi

print_header() {
    echo ""
    echo -e "${BLUE}🦉 OpenOwl — Personal Autonomous Agent${NC}"
    echo -e "${BLUE}══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

log_info()  { echo -e "${CYAN}ℹ${NC}  $1"; }
log_ok()    { echo -e "${GREEN}✅${NC} $1"; }
log_warn()  { echo -e "${YELLOW}⚠️${NC}  $1"; }
log_fail()  { echo -e "${RED}❌${NC} $1"; }

status_line() {
    local name="$1"
    local state="$2"
    case "$state" in
        UP)      echo -e "  ${GREEN}●${NC} ${name}: ${GREEN}UP${NC}" ;;
        WAITING) echo -e "  ${YELLOW}●${NC} ${name}: ${YELLOW}WAITING${NC}" ;;
        FAILED)  echo -e "  ${RED}●${NC} ${name}: ${RED}FAILED${NC}" ;;
        *)       echo -e "  ${BLUE}●${NC} ${name}: ${state}" ;;
    esac
}

require_env_file() {
    if [ ! -f .env ]; then
        log_warn ".env not found. Creating from .env.example"
        cp .env.example .env
        log_fail "Please edit .env first, then rerun: ./scripts/start.sh"
        exit 1
    fi
}

require_prereqs() {
    if ! command -v docker >/dev/null 2>&1; then
        log_fail "Docker not found. Install Docker Desktop first."
        exit 1
    fi

    if [ ${#COMPOSE[@]} -eq 0 ]; then
        log_fail "Docker Compose not found (docker compose or docker-compose)."
        exit 1
    fi

    if ! docker info >/dev/null 2>&1; then
        log_fail "Docker daemon is not running. Start Docker Desktop first."
        exit 1
    fi
}

container_health() {
    local container_name="$1"
    if ! docker ps --format '{{.Names}}' | grep -qx "$container_name"; then
        echo "FAILED"
        return
    fi

    local health
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_name" 2>/dev/null || echo "unknown")"
    case "$health" in
        healthy|none) echo "UP" ;;
        starting) echo "WAITING" ;;
        unhealthy) echo "FAILED" ;;
        *) echo "UP" ;;
    esac
}

wait_for_http() {
    local url="$1"
    local name="$2"
    local retries="${3:-30}"
    local i

    for ((i=1; i<=retries; i++)); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            status_line "$name" "UP"
            return 0
        fi
        status_line "$name" "WAITING"
        sleep 2
    done

    status_line "$name" "FAILED"
    return 1
}

start_ngrok() {
    if ! command -v ngrok >/dev/null 2>&1; then
        log_warn "ngrok not installed. Telegram webhook auto-tunnel skipped."
        status_line "ngrok tunnel" "FAILED"
        return 0
    fi

    if [ -f "$PID_FILE" ]; then
        local old_pid
        old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$old_pid" ] && kill -0 "$old_pid" >/dev/null 2>&1; then
            status_line "ngrok tunnel" "UP"
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    nohup ngrok http 8000 >"$NGROK_LOG" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"
    sleep 2

    if kill -0 "$new_pid" >/dev/null 2>&1; then
        status_line "ngrok tunnel" "UP"
    else
        status_line "ngrok tunnel" "FAILED"
        return 0
    fi

    local tunnel_url
    tunnel_url="$(curl -s http://127.0.0.1:4040/api/tunnels | python3 - <<'PY'
import json,sys
try:
        data=json.load(sys.stdin)
        tunnels=data.get('tunnels',[])
        https=[t.get('public_url','') for t in tunnels if t.get('public_url','').startswith('https://')]
        print(https[0] if https else '')
except Exception:
        print('')
PY
)"

    if [ -n "$tunnel_url" ]; then
        log_ok "ngrok URL: ${tunnel_url}"
        if ! grep -q '^TELEGRAM_WEBHOOK_URL=' .env; then
            echo "TELEGRAM_WEBHOOK_URL=${tunnel_url}" >> .env
            log_warn "Added TELEGRAM_WEBHOOK_URL to .env"
        elif grep -q '^TELEGRAM_WEBHOOK_URL=$' .env; then
            sed -i.bak "s|^TELEGRAM_WEBHOOK_URL=$|TELEGRAM_WEBHOOK_URL=${tunnel_url}|" .env
            log_warn "Updated empty TELEGRAM_WEBHOOK_URL in .env"
        fi
    else
        log_warn "ngrok started, but URL not detected yet (check http://127.0.0.1:4040)"
    fi
}

stop_ngrok() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
            kill "$pid" >/dev/null 2>&1 || true
            log_ok "Stopped ngrok (PID $pid)"
        fi
        rm -f "$PID_FILE"
    fi
}

start_stack() {
    print_header
    require_env_file
    require_prereqs

    local services=(postgres redis ollama openowl)
    if [ -f "$ROOT_DIR/tasks.py" ]; then
        services+=(worker)
        log_info "Worker detected (tasks.py found) — will start worker container"
    else
        log_warn "Worker not started (tasks.py missing)"
    fi

    log_info "Starting containers: ${services[*]}"
    if "${COMPOSE[@]}" up -d "${services[@]}" >/dev/null 2>&1; then
        log_ok "Docker services started"
    else
        log_fail "Failed to start Docker services"
        exit 1
    fi

    echo ""
    log_info "Service status"
    status_line "postgres" "$(container_health openowl-postgres)"
    status_line "redis" "$(container_health openowl-redis)"
    status_line "ollama" "$(container_health openowl-ollama)"
    status_line "openowl" "$(container_health openowl-core)"
    if [ -f "$ROOT_DIR/tasks.py" ]; then
        status_line "worker" "$(container_health openowl-worker)"
    else
        status_line "worker" "WAITING"
    fi

    echo ""
    log_info "Checking HTTP health"
    wait_for_http "http://127.0.0.1:8000/" "Landing page" 30 || true
    wait_for_http "http://127.0.0.1:8000/health" "Health endpoint" 30 || true

    echo ""
    log_info "Starting ngrok tunnel"
    start_ngrok

    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
    log_ok "OpenOwl startup sequence completed"
    echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  🌐 Landing:   ${BLUE}http://localhost:8000/${NC}"
    echo -e "  🖥️  Desktop:   ${BLUE}http://localhost:8000/dashboard${NC}"
    echo -e "  📋 Docs:      ${BLUE}http://localhost:8000/docs${NC}"
    echo -e "  ❤️  Health:    ${BLUE}http://localhost:8000/health${NC}"
    echo -e "  📄 ngrok log: ${BLUE}${NGROK_LOG}${NC}"
    echo ""
    echo -e "${YELLOW}Tip:${NC} Run ${BLUE}./scripts/start.sh status${NC} to see live UP/WAITING/FAILED states."
    echo -e "${YELLOW}Tip:${NC} Run ${BLUE}./scripts/start.sh logs${NC} to stream OpenOwl container logs."
    echo ""
}

show_status() {
    print_header
    require_prereqs

    log_info "Runtime status"
    status_line "postgres" "$(container_health openowl-postgres)"
    status_line "redis" "$(container_health openowl-redis)"
    status_line "ollama" "$(container_health openowl-ollama)"
    status_line "openowl" "$(container_health openowl-core)"
    if [ -f "$ROOT_DIR/tasks.py" ]; then
        status_line "worker" "$(container_health openowl-worker)"
    else
        status_line "worker" "WAITING"
    fi

    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        status_line "HTTP /health" "UP"
    else
        status_line "HTTP /health" "FAILED"
    fi

    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null || echo 0)" >/dev/null 2>&1; then
        status_line "ngrok tunnel" "UP"
    else
        status_line "ngrok tunnel" "FAILED"
    fi
}

stop_stack() {
    print_header
    require_prereqs

    log_info "Stopping ngrok"
    stop_ngrok

    log_info "Stopping containers: openowl, worker, ollama, redis, postgres"
    if "${COMPOSE[@]}" stop openowl worker ollama redis postgres >/dev/null 2>&1; then
        log_ok "Containers stopped"
    else
        log_warn "Some containers may already be stopped"
    fi
}

show_logs() {
    require_prereqs
    log_info "Streaming openowl-core logs (Ctrl+C to exit)"
    "${COMPOSE[@]}" logs -f openowl
}

command="${1:-start}"

case "$command" in
    start)
        start_stack
        ;;
    status)
        show_status
        ;;
    stop)
        stop_stack
        ;;
    restart)
        stop_stack
        start_stack
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "Usage: ./scripts/start.sh [start|status|stop|restart|logs]"
        exit 1
        ;;
esac
