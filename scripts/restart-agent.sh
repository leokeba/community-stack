#!/usr/bin/env bash
set -euo pipefail

unit=community-stack-agent.service
if ! load_state="$(sudo -n systemctl show "$unit" --property=LoadState --value)"; then
    printf 'Could not inspect %s.\n' "$unit" >&2
    exit 1
fi
if [[ "$load_state" == not-found ]]; then
    printf '%s is not installed; restart skipped.\n' "$unit"
    exit 0
fi

if ! started_before="$(sudo -n systemctl show "$unit" --property=ExecMainStartTimestampMonotonic --value)"; then
    printf 'Could not read the previous process start time for %s.\n' "$unit" >&2
    exit 1
fi
if [[ ! "$started_before" =~ ^[0-9]+$ ]]; then
    printf 'Could not verify the previous process start time for %s.\n' "$unit" >&2
    exit 1
fi

if ! sudo -n systemctl restart "$unit"; then
    printf 'Failed to restart %s after deployment.\n' "$unit" >&2
    exit 1
fi
if ! sudo -n systemctl is-active --quiet "$unit"; then
    printf '%s is not active after restart.\n' "$unit" >&2
    exit 1
fi

if ! started_after="$(sudo -n systemctl show "$unit" --property=ExecMainStartTimestampMonotonic --value)"; then
    printf 'Could not verify the restarted process start time for %s.\n' "$unit" >&2
    exit 1
fi
if [[ ! "$started_after" =~ ^[0-9]+$ ]] || ((started_after <= started_before)); then
    printf '%s did not report a newer process start time after restart.\n' "$unit" >&2
    exit 1
fi

printf '%s restarted and is active (process start time advanced).\n' "$unit"
