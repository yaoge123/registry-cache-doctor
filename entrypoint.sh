#!/bin/sh
# Entry point for the registry-cache-doctor container.
#
# Behaviour mirrors acme.sh's split-mode pattern:
#   * `daemon`     -> generate a crontab from $RCD_SCHEDULE and exec
#                     supercronic, which runs `rcd scan` (and optionally
#                     `rcd clean`) on the configured schedule.
#   * any other arg -> exec `rcd "$@"` directly (one-shot use).
#
# Deployment-level environment variables (consumed only here):
#   RCD_CONFIG              Path to the TOML configuration mounted into
#                           the container. Default /etc/rcd/config.toml.
#   RCD_SCHEDULE            cron expression supercronic understands.
#                           Default "0 3 * * *" (daily at 03:00).
#   RCD_DAEMON_AUTO_CLEAN   "true" to append `clean --apply` after each
#                           scan. Default "false" (scan-only).
#   RCD_DAEMON_STRICT       "true" to add `--strict` to the daemon's
#                           scheduled scan/clean invocations so cron
#                           exits 3 / 4 on real failures. Default
#                           "false" (drift-2 is rewritten to 0 by the
#                           rcd-cron-exec wrapper, anything beyond that
#                           propagates).
#
# Behavioural defaults that travel with the configuration live in the
# TOML file mounted at $RCD_CONFIG.
set -eu

: "${RCD_CONFIG:=/etc/rcd/config.toml}"
: "${RCD_SCHEDULE:=0 3 * * *}"
: "${RCD_DAEMON_AUTO_CLEAN:=false}"
: "${RCD_DAEMON_STRICT:=false}"

if [ "${1:-}" = "daemon" ]; then
    crontab_file="$(mktemp)"
    trap 'rm -f "$crontab_file"' EXIT

    if [ "$RCD_DAEMON_STRICT" = "true" ]; then
        strict_flag=" --strict"
    else
        strict_flag=""
    fi

    # Always scan; optionally also clean.  Both invocations are wrapped in
    # rcd-cron-exec so the informational "drift detected" exit (2) is
    # translated to 0 and supercronic does not log a misleading
    # `level=error` line.  Genuine failures (1/3/4 etc.) propagate.
    {
        printf '%s rcd-cron-exec rcd --config %s scan%s\n' \
            "$RCD_SCHEDULE" "$RCD_CONFIG" "$strict_flag"
        if [ "$RCD_DAEMON_AUTO_CLEAN" = "true" ]; then
            printf '%s rcd-cron-exec rcd --config %s clean --apply%s\n' \
                "$RCD_SCHEDULE" "$RCD_CONFIG" "$strict_flag"
        fi
    } >"$crontab_file"

    echo "rcd: starting supercronic with schedule '$RCD_SCHEDULE'" >&2
    exec /usr/local/bin/supercronic -passthrough-logs "$crontab_file"
fi

exec rcd "$@"
