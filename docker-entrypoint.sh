#!/bin/bash
# =============================================================================
# Invoice Pipeline — Docker Entrypoint
# =============================================================================
#
# Behaviour:
#
#   WITH arguments  →  passed straight through to main.py (explicit commands
#                      always win, regardless of WATCH_MODE):
#
#       docker compose run --rm pipeline check
#       docker compose run --rm pipeline process /app/invoices
#       docker compose run --rm pipeline watch /app/invoices --interval 60
#
#   WITHOUT arguments  →  mode is chosen by the WATCH_MODE environment variable:
#
#       WATCH_MODE=false (default)
#           Runs a single batch pass over /app/invoices then exits.
#           Suitable for:  docker compose run --rm pipeline
#                          cron / scheduled task
#
#       WATCH_MODE=true
#           Continuously polls /app/invoices and processes new PDFs.
#           Suitable for:  docker compose up -d
#                          long-running service
#
# =============================================================================
set -e

# If any arguments were supplied, forward them directly and exit.
if [ "$#" -gt 0 ]; then
    exec python main.py "$@"
fi

# No arguments — choose mode from WATCH_MODE (default: false).
WATCH_MODE="${WATCH_MODE:-false}"

if [ "${WATCH_MODE}" = "true" ] || [ "${WATCH_MODE}" = "1" ]; then
    echo ""
    echo "  Mode:      watch (WATCH_MODE=true)"
    echo "  Directory: /app/invoices"
    echo "  Interval:  every ${POLL_INTERVAL:-30}s"
    echo "  Model:     ${OLLAMA_MODEL:-llama3.2}"
    echo "  Output:    /app/output/"
    echo ""
    echo "  Press Ctrl-C (or docker compose stop) to exit cleanly."
    echo ""
    exec python main.py watch /app/invoices
else
    echo ""
    echo "  Mode:      batch (WATCH_MODE=false)"
    echo "  Directory: /app/invoices"
    echo "  Model:     ${OLLAMA_MODEL:-llama3.2}"
    echo "  Output:    /app/output/"
    echo ""
    exec python main.py process /app/invoices
fi
