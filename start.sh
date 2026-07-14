#!/usr/bin/env bash
# Launch k_worker and d_worker side by side.
# Supervisor: if either exits, both are stopped.

set -euo pipefail

trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM

echo "[start.sh] Launching k_worker..."
python -m k_worker &
K_PID=$!

echo "[start.sh] Launching d_worker..."
python -m d_worker &
D_PID=$!

echo "[start.sh] Running — k_worker=$K_PID d_worker=$D_PID"

wait -n 2>/dev/null || true
echo "[start.sh] A process exited — shutting down both."
kill $K_PID $D_PID 2>/dev/null || true
wait
echo "[start.sh] Done."
