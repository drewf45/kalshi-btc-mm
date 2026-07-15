#!/usr/bin/env bash
# Launch d_worker — the sole process.
# k_worker: decommissioned (crypto lane sunset). Crypto series blacklisted.
# d_worker: scanner (outside body) + watchdog (inside body).

set -euo pipefail

trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM

echo "[start.sh] Launching d_worker..."
python -m d_worker &
D_PID=$!

echo "[start.sh] Running — d_worker=$D_PID"

wait $D_PID
echo "[start.sh] d_worker exited."
