#!/usr/bin/env bash
# Transcription service manager
# Uses the full script path as a unique process-name marker — no pidfile needed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN="$SCRIPT_DIR/main.py"
LOGFILE="$SCRIPT_DIR/collect.log"
PYTHON="/home/test/.virtualenvs/collect/bin/python3"
PIP="/home/test/.virtualenvs/collect/bin/pip"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

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
    # Find PID of whatever is listening on our PORT.
    # PORT is loaded from .env (line 13), so it always matches the running service.
    lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1
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

configure_env() {
    echo ""
    echo "  Configure environment (.env)"
    echo "  ─────────────────────────────────"

    # Read existing values as defaults
    local cur_url
    cur_url=$(grep -E '^USER_DATABASE_URL=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2-)

    # Parse existing DSN if present
    local def_host="localhost" def_port="5432" def_db="mzhu_test_autism_users" def_user="dbuser" def_pass=""
    if [ -n "$cur_url" ]; then
        # postgresql://user:pass@host:port/db
        def_user=$(echo "$cur_url" | sed -E 's|postgresql://([^:@]+).*|\1|')
        def_pass=$(echo "$cur_url" | sed -E 's|postgresql://[^:]+:([^@]*)@.*|\1|')
        def_host=$(echo "$cur_url" | sed -E 's|.*@([^:/]+)[:/].*|\1|')
        def_port=$(echo "$cur_url" | sed -E 's|.*:([0-9]+)/.*|\1|')
        def_db=$(echo "$cur_url"   | sed -E 's|.*/([^/]+)$|\1|')
    fi

    local cur_port cur_model cur_lang
    cur_port=$(grep -E '^PORT='            "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2-)
    cur_model=$(grep -E '^WHISPER_MODEL='  "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2-)
    cur_lang=$(grep  -E '^WHISPER_LANGUAGE=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2-)

    local def_api_port="${cur_port:-18001}"
    local def_model="${cur_model:-base}"
    local def_lang="${cur_lang:-}"

    printf "  DB host     [%s]: " "$def_host";  read -r inp; DB_HOST="${inp:-$def_host}"
    printf "  DB port     [%s]: " "$def_port";  read -r inp; DB_PORT="${inp:-$def_port}"
    printf "  DB name     [%s]: " "$def_db";    read -r inp; DB_NAME="${inp:-$def_db}"
    printf "  DB user     [%s]: " "$def_user";  read -r inp; DB_USER="${inp:-$def_user}"
    printf "  DB password [%s]: " "${def_pass:+(set)}"; read -rs inp; echo
    DB_PASS="${inp:-$def_pass}"
    printf "  API port    [%s]: " "$def_api_port"; read -r inp; API_PORT="${inp:-$def_api_port}"
    printf "  Whisper model [%s]: " "$def_model"; read -r inp; W_MODEL="${inp:-$def_model}"
    printf "  Whisper language (blank=auto) [%s]: " "$def_lang"; read -r inp; W_LANG="${inp:-$def_lang}"

    local dsn="postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

    {
        echo "USER_DATABASE_URL=${dsn}"
        echo "PORT=${API_PORT}"
        echo "WHISPER_MODEL=${W_MODEL}"
        [ -n "$W_LANG" ] && echo "WHISPER_LANGUAGE=${W_LANG}"
    } > "$SCRIPT_DIR/.env"

    echo ""
    echo "  ✅  .env written"
    echo "  🔗  DSN: ${dsn}"
    echo ""
}

run_migration() {
    echo ""
    local dsn
    dsn=$(grep -E '^USER_DATABASE_URL=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
    if [ -z "$dsn" ]; then
        echo "  ❌  USER_DATABASE_URL not found in .env — cannot run migration"
        echo ""
        return
    fi

    local migrations_dir="$SCRIPT_DIR/migrations"
    local files
    files=$(ls "$migrations_dir"/*.sql 2>/dev/null | sort)
    if [ -z "$files" ]; then
        echo "  ❌  No migration files found in $migrations_dir"
        echo ""
        return
    fi

    # Parse DSN components so PGPASSWORD is always set explicitly
    local db_user db_pass db_host db_port db_name
    db_user=$(echo "$dsn" | sed -E 's|postgresql://([^:@]+).*|\1|')
    db_pass=$(echo "$dsn" | sed -E 's|postgresql://[^:]+:([^@]*)@.*|\1|')
    db_host=$(echo "$dsn" | sed -E 's|.*@([^:/]+)[:/].*|\1|')
    db_port=$(echo "$dsn" | sed -E 's|.*:([0-9]+)/.*|\1|')
    db_name=$(echo "$dsn" | sed -E 's|.*/([^/]+)$|\1|')

    # psql shorthand reused for all calls
    _psql() { PGPASSWORD="$db_pass" psql -h "$db_host" -p "$db_port" -U "$db_user" "$@"; }

    # Ensure the target database exists
    printf "      %-45s " "database '$db_name'"
    if _psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db_name'" 2>/dev/null | grep -q 1; then
        echo "✅  (exists)"
    else
        # Try as dbuser first; fall back to postgres superuser via peer auth
        local created=0
        if _psql -d postgres -c "CREATE DATABASE \"$db_name\"" > /dev/null 2>&1; then
            created=1
        elif sudo -u postgres psql -c "CREATE DATABASE \"$db_name\"" > /dev/null 2>&1; then
            created=1
            # Grant schema privileges so dbuser can create tables
            sudo -u postgres psql -d "$db_name" \
                -c "GRANT ALL ON SCHEMA public TO \"$db_user\"" > /dev/null 2>&1
        fi
        if [ "$created" -eq 1 ]; then
            echo "✅  (created)"
        else
            echo "❌"
            sudo -u postgres psql -c "CREATE DATABASE \"$db_name\"" 2>&1 | sed 's/^/        /'
            echo ""
            echo "  ❌  Cannot create database — aborting migrations"
            echo ""
            return
        fi
    fi

    echo "  🗄️   Running DB migrations against: $dsn"
    local failed=0
    for sql in $files; do
        printf "      %-45s " "$(basename "$sql")"
        if _psql -d "$db_name" -f "$sql" > /dev/null 2>&1; then
            echo "✅"
        else
            echo "❌"
            failed=1
            _psql -d "$db_name" -f "$sql" 2>&1 | sed 's/^/        /'
        fi
    done

    echo ""
    if [ "$failed" -eq 0 ]; then
        echo "  ✅  All migrations complete"
    else
        echo "  ❌  One or more migrations failed — see output above"
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

install_requirements() {
    echo ""
    if [ ! -f "$REQUIREMENTS" ]; then
        echo "  ❌  requirements.txt not found at $REQUIREMENTS"
        echo ""
        return
    fi
    if [ ! -x "$PIP" ]; then
        echo "  ❌  pip not found at $PIP"
        echo ""
        return
    fi
    echo "  📦  Installing requirements from $REQUIREMENTS..."
    if "$PIP" install -r "$REQUIREMENTS"; then
        echo "  ✅  Requirements installed"
    else
        echo "  ❌  Failed to install requirements"
    fi
    echo ""
}

# ── Menu ───────────────────────────────────────────────────────────────────────

while true; do
    echo "╔══════════════════════════════════════╗"
    echo "║       Collect Service                ║"
    echo "╠══════════════════════════════════════╣"
    echo "║  1) Start / Restart service          ║"
    echo "║  2) Stop service                     ║"
    echo "║  3) Service status                   ║"
    echo "║  4) Run DB migration                 ║"
    echo "║  5) Configure environment (.env)     ║"
    echo "║  6) Install requirements             ║"
    echo "║  0) Exit                             ║"
    echo "╚══════════════════════════════════════╝"
    printf "  Choose an option: "
    read -r choice

    case "$choice" in
        1) start_service         ;;
        2) stop_service          ;;
        3) service_status        ;;
        4) run_migration         ;;
        5) configure_env         ;;
        6) install_requirements  ;;
        0) echo ""; echo "  Bye!"; echo ""; exit 0 ;;
        *) echo ""; echo "  ⚠️  Invalid option"; echo "" ;;
    esac
done
