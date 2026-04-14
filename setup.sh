#!/usr/bin/env bash
# Transcription service manager
# Uses the full script path as a unique process-name marker — no pidfile needed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$SCRIPT_DIR/main.py"
LOGFILE="$SCRIPT_DIR/collect.log"
PYTHON="/home/test/.virtualenvs/collect/bin/python3"

# Load PORT from .env (fallback to default)
PORT=$(grep -E '^PORT=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
PORT=${PORT:-18001}

get_local_ip() {
    # Try Linux first, then macOS, then fall back to localhost
    ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' \
    || ipconfig getifaddr en0 2>/dev/null \
    || ipconfig getifaddr en1 2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}' \
    || echo "127.0.0.1"
}

# ── Process helpers ────────────────────────────────────────────────────────────

service_pid() {
    # Find PID of the venv python process running our exact main.py path
    pgrep -f "$PYTHON $MAIN" | head -1
}

is_running() {
    [ -n "$(service_pid)" ]
}

# ── Actions ────────────────────────────────────────────────────────────────────

start_service() {
    echo ""
    if is_running; then
        echo "  ↻  Already running (PID $(service_pid)) — restarting..."
        stop_service
    fi

    cd "$SCRIPT_DIR" || exit 1
    nohup "$PYTHON" "$MAIN" >> "$LOGFILE" 2>&1 &

    # Wait up to 6 s for process to appear
    local i=0
    while [ $i -lt 12 ]; do
        is_running && break
        sleep 0.5
        i=$((i + 1))
    done

    if is_running; then
        local ip
        ip=$(get_local_ip)
        echo "  ✅  Service started (PID $(service_pid))"
        echo "  🌐  https://$ip:$PORT/"
        echo "  📄  Logs: $LOGFILE"
    else
        echo "  ❌  Failed to start — check $LOGFILE"
        tail -8 "$LOGFILE" 2>/dev/null | sed 's/^/      /'
    fi
    echo ""
}

stop_service() {
    echo ""
    local pid
    pid=$(service_pid)

    if [ -z "$pid" ]; then
        echo "  ⚠️   Service is not running"
        echo ""
        return
    fi

    echo "  🛑  Stopping PID $pid..."
    kill "$pid" 2>/dev/null

    # Wait up to 5 s for process to exit
    local i=0
    while [ $i -lt 10 ]; do
        sleep 0.5
        is_running || break
        i=$((i + 1))
    done

    # Escalate to SIGKILL if still alive
    if is_running; then
        echo "  ⚡  SIGTERM timed out — sending SIGKILL..."
        kill -9 "$(service_pid)" 2>/dev/null
        sleep 0.5
    fi

    if is_running; then
        echo "  ❌  Could not stop service (PID $(service_pid) still running)"
    else
        echo "  ✅  Service stopped"
    fi
    echo ""
}

service_status() {
    echo ""
    local pid
    pid=$(service_pid)

    if [ -n "$pid" ]; then
        local ip
        ip=$(get_local_ip)
        echo "  ✅  Service is running"
        echo "  🔢  PID:   $pid"
        echo "  🌐  URL:   https://$ip:$PORT/"

        # Uptime from /proc
        if [ -f "/proc/$pid/stat" ]; then
            local start_ticks btime elapsed hz
            start_ticks=$(awk '{print $22}' /proc/$pid/stat 2>/dev/null)
            btime=$(awk '/^btime/{print $2}' /proc/stat 2>/dev/null)
            hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
            if [ -n "$start_ticks" ] && [ -n "$btime" ]; then
                elapsed=$(( $(date +%s) - btime - start_ticks / hz ))
                local h=$((elapsed/3600)) m=$(( (elapsed%3600)/60 )) s=$((elapsed%60))
                printf "  ⏱️   Up:   %dh %02dm %02ds\n" "$h" "$m" "$s"
            fi
        fi

        # HTTP health check
        local http_code
        http_code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 3 \
                    "https://127.0.0.1:$PORT/health" 2>/dev/null)
        if [ "$http_code" = "200" ]; then
            local model
            model=$(curl -sk --max-time 3 "https://127.0.0.1:$PORT/health" 2>/dev/null \
                    | grep -oP '"model"\s*:\s*"\K[^"]+')
            echo "  💚  HTTP: OK (200) — model: ${model:-?}"
        else
            echo "  ⚠️   HTTP: no response (HTTP ${http_code:-timeout})"
        fi
    else
        echo "  🔴  Service is NOT running"
    fi

    # Last 6 log lines (always shown if log exists)
    if [ -f "$LOGFILE" ] && [ -s "$LOGFILE" ]; then
        echo "  ─────────────────────────────────"
        echo "  📄  Last log lines:"
        tail -6 "$LOGFILE" | sed 's/^/      /'
    fi
    echo ""
}

# ── Menu ───────────────────────────────────────────────────────────────────────

while true; do
    echo "╔══════════════════════════════════════╗"
    echo "║    Voice Transcription Service       ║"
    echo "╠══════════════════════════════════════╣"
    echo "║  1) Start / Restart service          ║"
    echo "║  2) Stop service                     ║"
    echo "║  3) Service status                   ║"
    echo "║  0) Exit                             ║"
    echo "╚══════════════════════════════════════╝"
    printf "  Choose an option: "
    read -r choice

    case "$choice" in
        1) start_service  ;;
        2) stop_service   ;;
        3) service_status ;;
        0) echo ""; echo "  Bye!"; echo ""; exit 0 ;;
        *) echo ""; echo "  ⚠️  Invalid option"; echo "" ;;
    esac
done
