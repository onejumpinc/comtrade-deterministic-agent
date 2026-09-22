#!/bin/sh
set -eu

# The leaderboard shares this named volume with the green agent. Normalize its
# ownership at startup, then run the network-facing process without root.
if [ "$(id -u)" = "0" ]; then
  chown agent:agent /workspace/purple_output
  exec gosu agent python -u /app/run_a2a.py "$@"
fi

exec python -u /app/run_a2a.py "$@"
