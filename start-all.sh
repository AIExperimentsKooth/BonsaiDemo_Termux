#!/data/data/com.termux/files/usr/bin/sh
# Idempotent launcher for the Bonsai stack: llama-server + agent harness.
#
# Safe to run repeatedly (on every Termux launch, or by hand): only missing
# processes are started. Everything is detached with setsid+nohup so it
# survives the launching shell closing.

LLAMA_DIR="$HOME/Bonsai-demo"
HARNESS_DIR="$HOME/agent-harness"
WRAPPER="$HARNESS_DIR/start_llama_tight.sh"
LOG="$HOME/autostart.log"
LOCK_DIR="$HOME/.bonsai-autostart.lock"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"
}

# --- single-instance guard -------------------------------------------------
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -d "$LOCK_DIR" ] || break
        sleep 1
    done
    if [ -d "$LOCK_DIR" ]; then
        log "start-all: another instance still holds the lock, exiting"
        exit 0
    fi
    mkdir "$LOCK_DIR" 2>/dev/null || exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM

running() {
    ps -A -o args 2>/dev/null | grep -q "$1"
}

# --- llama-server ----------------------------------------------------------
if running '[l]lama-server'; then
    log "start-all: llama-server already running"
elif [ -f "$WRAPPER" ]; then
    log "start-all: starting llama-server via start_llama_tight.sh"
    (
        cd "$LLAMA_DIR" || exit 1
        setsid nohup sh "$WRAPPER" >>"$HOME/llama-server.log" 2>&1 </dev/null &
    )
else
    log "start-all: MISSING $WRAPPER"
fi

# --- agent harness ---------------------------------------------------------
if running '[p]ython3 app.py'; then
    log "start-all: harness already running"
elif [ -f "$HARNESS_DIR/app.py" ]; then
    log "start-all: starting harness on 0.0.0.0:8081"
    (
        cd "$HARNESS_DIR" || exit 1
        setsid nohup python3 app.py \
            --host 0.0.0.0 --port 8081 \
            --server-script "$WRAPPER" \
            --server-log "$HOME/llama-server.log" \
            >>"$HOME/harness.log" 2>&1 </dev/null &
    )
else
    log "start-all: MISSING $HARNESS_DIR/app.py"
fi
