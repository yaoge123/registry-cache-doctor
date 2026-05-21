#!/bin/sh
# Entry point for the registry-cache-doctor container.
#
# Behaviour mirrors acme.sh's split-mode pattern:
#   * `daemon`     -> generate a crontab from $RCD_SCHEDULE and exec
#                     supercronic, which runs `rcd scan` (and optionally
#                     `rcd clean`) on the configured schedule.
#   * any other arg -> exec `rcd "$@"` directly (one-shot use).
#
# Defaults are intentionally conservative; tweak via env or via the
# TOML config mounted at $RCD_CONFIG.
set -eu

: "${RCD_CONFIG:=/etc/rcd/config.toml}"
: "${RCD_SCHEDULE:=0 3 * * *}"
: "${RCD_DAEMON_AUTO_CLEAN:=false}"

if [ "${1:-}" = "daemon" ]; then
    crontab_file="$(mktemp)"
    trap 'rm -f "$crontab_file"' EXIT

    # Always scan; optionally also clean.
    {
        printf '%s rcd --config %s scan\n' "$RCD_SCHEDULE" "$RCD_CONFIG"
        if [ "$RCD_DAEMON_AUTO_CLEAN" = "true" ]; then
            printf '%s rcd --config %s clean --apply\n' "$RCD_SCHEDULE" "$RCD_CONFIG"
        fi
    } >"$crontab_file"

    echo "rcd: starting supercronic with schedule '$RCD_SCHEDULE'" >&2
    exec /usr/local/bin/supercronic -passthrough-logs "$crontab_file"
fi

exec rcd "$@"
