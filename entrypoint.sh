#!/bin/sh
# Container entrypoint. Starts the proxy bridge in the background, then runs
# gunicorn in the foreground. If either process dies, the container exits so
# Docker's restart policy can recover.
#
# The bridge supports two modes:
#   direct — uses ANTHROPIC_API_KEY to call api.anthropic.com directly (default
#             when PROXY_* vars are not set).
#   proxy  — uses PROXY_UPSTREAM_URL + PROXY_AUTH_TOKEN + PROXY_STREAM_URL_TEMPLATE
#             to forward through an upstream proxy.
# At least one mode must be configured.

set -e

log() { printf '[entrypoint] %s\n' "$*"; }

# Forward SIGTERM/SIGINT to both children for clean shutdown.
shutdown() {
    log "shutdown signal received"
    if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
        kill -TERM "$BRIDGE_PID" 2>/dev/null || true
    fi
    if [ -n "$GUNICORN_PID" ] && kill -0 "$GUNICORN_PID" 2>/dev/null; then
        kill -TERM "$GUNICORN_PID" 2>/dev/null || true
    fi
    wait
    exit 0
}
trap shutdown TERM INT

log "starting bridge"
python -u /app/bridge/bridge.py &
BRIDGE_PID=$!
log "bridge pid=$BRIDGE_PID"

# Wait for bridge to bind its port before starting gunicorn.
i=0
while [ $i -lt 30 ]; do
    if curl -sf "http://127.0.0.1:${BRIDGE_FAKE_PORT:-9999}/health" >/dev/null 2>&1; then
        log "bridge ready (mode=$(curl -sf http://127.0.0.1:${BRIDGE_FAKE_PORT:-9999}/mode | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("mode","?"))' 2>/dev/null || echo "?"))"
        break
    fi
    # Check if the bridge process already died (misconfigured env).
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        log "bridge process exited immediately — check env vars (need ANTHROPIC_API_KEY or PROXY_* vars)"
        exit 1
    fi
    i=$((i + 1))
    sleep 1
done
if [ $i -ge 30 ]; then
    log "bridge failed to become healthy within 30s; aborting"
    kill -TERM "$BRIDGE_PID" 2>/dev/null || true
    exit 1
fi

log "starting gunicorn"
gunicorn --config gunicorn.conf.py app:app &
GUNICORN_PID=$!
log "gunicorn pid=$GUNICORN_PID"

# Exit when either child dies.
while true; do
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        log "bridge exited; tearing down container"
        kill -TERM "$GUNICORN_PID" 2>/dev/null || true
        wait
        exit 1
    fi
    if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
        log "gunicorn exited; tearing down container"
        kill -TERM "$BRIDGE_PID" 2>/dev/null || true
        wait
        exit 1
    fi
    sleep 2
done
