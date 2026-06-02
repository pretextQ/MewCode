#!/usr/bin/env bash
# MewCode tmux-based E2E smoke test helper.
# Usage:
#   ./scripts/mewtest.sh start              — start MewCode in tmux
#   ./scripts/mewtest.sh send "message"     — send a message
#   ./scripts/mewtest.sh capture            — capture current screen
#   ./scripts/mewtest.sh wait [timeout]     — wait until response completes (default 60s)
#   ./scripts/mewtest.sh sendwait "msg" [t] — send + wait + capture
#   ./scripts/mewtest.sh stop               — kill the session
#   ./scripts/mewtest.sh smoke              — full smoke test: start → send → verify → stop

set -euo pipefail

SESSION="mewtest"
MEWCODE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BINARY="$MEWCODE_DIR/mewcode"
WIDTH=120
HEIGHT=40

start() {
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    tmux new-session -d -s "$SESSION" -x "$WIDTH" -y "$HEIGHT" \
        "cd '$MEWCODE_DIR' && '$BINARY'"

    local timeout=10
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        if tmux capture-pane -t "$SESSION" -p 2>/dev/null | grep -q "Send a message"; then
            echo "MewCode started successfully"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    echo "ERROR: MewCode did not start within ${timeout}s"
    capture
    return 1
}

send() {
    local msg="$1"
    tmux send-keys -t "$SESSION" "$msg" Enter
}

capture() {
    tmux capture-pane -t "$SESSION" -p 2>/dev/null | sed 's/[[:space:]]*$//'
}

wait_response() {
    local timeout="${1:-60}"
    local elapsed=0
    local prev_output=""
    local stable_count=0

    sleep 2
    while [ $elapsed -lt $timeout ]; do
        local current
        current="$(capture)"

        # MewCode shows "✻ <PastTenseVerb> for X.Xs" when done.
        # There are 110+ random verbs so we match the ✻ ... for pattern.
        if echo "$current" | grep -qP '✻ .+ for [0-9]'; then
            if [ "$current" = "$prev_output" ]; then
                stable_count=$((stable_count + 1))
                if [ $stable_count -ge 2 ]; then
                    echo "$current"
                    return 0
                fi
            else
                stable_count=0
            fi
        fi

        prev_output="$current"
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "TIMEOUT after ${timeout}s — last captured output:"
    capture
    return 1
}

sendwait() {
    local msg="$1"
    local timeout="${2:-60}"
    send "$msg"
    wait_response "$timeout"
}

stop() {
    tmux send-keys -t "$SESSION" C-c 2>/dev/null || true
    sleep 1
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "MewCode session stopped"
}

smoke() {
    echo "=== MewCode Smoke Test ==="

    echo "[1/4] Starting MewCode..."
    start

    echo "[2/4] Sending test message..."
    local output
    output="$(sendwait "Reply with exactly: SMOKE_TEST_OK" 30)"

    if echo "$output" | grep -q "SMOKE_TEST_OK"; then
        echo "[3/4] Response received: PASS"
    else
        echo "[3/4] Response check: FAIL — expected SMOKE_TEST_OK"
        echo "$output"
        stop
        return 1
    fi

    echo "[4/4] Cleaning up..."
    stop

    echo "=== Smoke Test PASSED ==="
}

case "${1:-help}" in
    start)     start ;;
    send)      send "${2:?usage: mewtest.sh send \"message\"}" ;;
    capture)   capture ;;
    wait)      wait_response "${2:-60}" ;;
    sendwait)  sendwait "${2:?usage: mewtest.sh sendwait \"message\" [timeout]}" "${3:-60}" ;;
    stop)      stop ;;
    smoke)     smoke ;;
    *)
        echo "Usage: $0 {start|send|capture|wait|sendwait|stop|smoke}"
        echo ""
        echo "Commands:"
        echo "  start              Start MewCode in tmux"
        echo "  send \"message\"     Send a message"
        echo "  capture            Capture current screen"
        echo "  wait [timeout]     Wait for response to complete"
        echo "  sendwait \"msg\" [t] Send, wait, and capture"
        echo "  stop               Kill the session"
        echo "  smoke              Full smoke test"
        ;;
esac
