#!/usr/bin/env bash
# --------------------------------------------------------------------------
# stop.sh — Tear down the immune system infrastructure
#
# Usage:
#   ./stop.sh           Stop containers, keep data volumes
#   ./stop.sh --clean   Stop containers AND remove data volumes
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CLEAN=false
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=true ;;
    esac
done

echo "▶ Stopping immune system containers …"

if [[ "$CLEAN" == true ]]; then
    docker compose down -v 2>/dev/null || true
    echo "  Containers stopped and volumes removed."
else
    docker compose down 2>/dev/null || true
    echo "  Containers stopped. Data volumes preserved."
    echo "  Run with --clean to also remove volumes."
fi

echo "Done."
