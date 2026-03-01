#!/bin/bash
# =============================================================================
# Invoice Pipeline — Docker Entrypoint
# =============================================================================
#
# Privilege handling (gosu pattern):
#   The entrypoint always starts as root (no `user:` in compose).
#   Set PUID / PGID in your .env to match your host user — the entrypoint
#   chowns all mounted volumes to that UID:GID, then exec's the final process
#   directly via gosu (no script re-execution).
#
#   PUID=1000  # host user UID  (default: 1000)
#   PGID=1000  # host user GID  (default: 1000)
#
# Behaviour:
#
#   WITH arguments  →  passed straight through:
#
#       docker compose run --rm pipeline check
#       docker compose run --rm pipeline process /app/invoices
#       docker compose run --rm pipeline process /app/invoices/my_invoice.pdf
#       docker compose run --rm pipeline watch /app/invoices --interval 60
#
#   WITHOUT arguments  →  mode is chosen by WATCH_MODE:
#
#       WATCH_MODE=true (default)
#           Continuously polls /app/invoices and processes new PDFs.
#           Suitable for:  docker compose up -d
#
#       WATCH_MODE=false
#           Runs a single batch pass over /app/invoices then exits.
#
# =============================================================================
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# -----------------------------------------------------------------------------
# Root-only setup — fix ownership of all mounted volumes, then drop privileges
# -----------------------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    echo "[entrypoint] Running as root — fixing volume ownership (${PUID}:${PGID})"

    # Ensure all mounted directories exist
    mkdir -p \
        /app/data \
        /app/config \
        /app/invoices \
        /app/output/export \
        /app/backups \
        /app/.cache/docling

    # Bootstrap config files first (as root, so it can write anywhere).
    # Must run before chown so that any files it creates are caught by the
    # ownership fix below rather than being left root-owned.
    python3 /app/bootstrap.py

    # Transfer ownership of every mounted path to the app user.
    # Runs on every startup so all files — including those just created by
    # bootstrap — are owned by PUID:PGID before the app process starts.
    chown -R "${PUID}:${PGID}" \
        /app/data \
        /app/config \
        /app/invoices \
        /app/output \
        /app/backups \
        /app/.cache/docling

    echo "[entrypoint] Dropping to UID=${PUID} GID=${PGID}"
fi

# -----------------------------------------------------------------------------
# Resolve the command to run (used by both root→gosu and direct non-root paths)
# -----------------------------------------------------------------------------

# If arguments were supplied, route them:
#   check / process / watch  →  main.py subcommands
#   anything else            →  exec directly (e.g. python3 -m uvicorn ...)
if [ "$#" -gt 0 ]; then
    case "$1" in
        check|process|watch)
            CMD=(python3 main.py "$@")
            ;;
        *)
            CMD=("$@")
            ;;
    esac
else
    # No arguments — choose mode from WATCH_MODE
    WATCH_MODE="${WATCH_MODE:-false}"
    if [ "${WATCH_MODE}" = "true" ] || [ "${WATCH_MODE}" = "1" ]; then
        echo ""
        echo "  Mode:      watch (WATCH_MODE=true)"
        echo "  Directory: /app/invoices"
        echo "  Interval:  every ${POLL_INTERVAL:-30}s"
        echo "  Model:     ${LLM_MODEL:-llama3.2}"
        echo "  Output:    /app/output/"
        echo ""
        echo "  Press Ctrl-C (or docker compose stop) to exit cleanly."
        echo ""
        CMD=(python3 main.py watch /app/invoices)
    else
        echo ""
        echo "  Mode:      batch (WATCH_MODE=false)"
        echo "  Directory: /app/invoices"
        echo "  Model:     ${LLM_MODEL:-llama3.2}"
        echo "  Output:    /app/output/"
        echo ""
        CMD=(python3 main.py process /app/invoices)
    fi
fi

# -----------------------------------------------------------------------------
# Exec the final process — as the app user if we started as root, else directly
# -----------------------------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    exec gosu "${PUID}:${PGID}" "${CMD[@]}"
else
    exec "${CMD[@]}"
fi
