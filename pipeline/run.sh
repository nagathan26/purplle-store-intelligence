#!/usr/bin/env bash
# run.sh — clear DB, process all CCTV clips on GPU/CPU and feed the API.
#
# Usage:
#   ./pipeline/run.sh
#
set -euo pipefail
API="${API_URL:-http://localhost:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$(dirname "$HERE")"

echo "[run] starting CCTV detection and feeding into API at $API"
python "$HERE/seed.py" --wait --api "$API"

echo "[run] done. Dashboard: $API/"
