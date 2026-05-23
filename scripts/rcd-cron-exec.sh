#!/bin/sh
# rcd-cron-exec: wrapper for cron-driven `rcd scan` invocations.
#
# rcd's exit code 2 means "drift detected" -- a normal, informational
# outcome that does not indicate a tool failure.  supercronic, however,
# logs any non-zero child exit as `level=error msg="error running command:
# exit status 2"`, which fans out to the log/alert pipeline as a false
# positive.
#
# Wrap the invocation with this script in the crontab entries so that the
# only "drift" exit (2) is normalised to 0, while genuine failures (1) and
# strict-mode codes (3, 4) and any other unexpected codes propagate
# verbatim.
#
# Usage:
#     rcd-cron-exec <command> [args ...]
#
# Behaviour:
#     0   -> 0     (no drift)
#     1   -> 1     (tool/connection error)
#     2   -> 0     (drift detected; informational)
#     3   -> 3     (--strict + real failure)
#     4   -> 4     (--strict + clean failed)
#     *   -> *     (any other code passed through verbatim)
set -u

if [ "$#" -lt 1 ]; then
    echo "usage: rcd-cron-exec <command> [args ...]" >&2
    exit 64  # EX_USAGE
fi

# Run the command without `set -e` so we can capture its exit status.
"$@"
rc=$?

if [ "$rc" -eq 2 ]; then
    exit 0
fi

exit "$rc"
