#!/usr/bin/env bash
# listener-health.sh: the health check of a disposable-container lane's scale-set listener.
#
#   listener-health.sh <name>    check <name>-listener.service, restart it once when it is down or
#                                silent, write the verdict, exit 1 while unhealthy
#
# <name>-listener-health.timer runs it as root every 5 minutes; bin/provision-lane.sh writes the
# timer for every lane and its --check prints the verdict. systemd restarts a listener that crashes
# (Restart=always), but not one that runs and has stopped working: a wedged long poll, or an App
# permission GitHub answers with 401 or 403 on every call. The listener logs a heartbeat every
# minute (listener/README.md, "Logging"), so a journal silent for 3 minutes means it stopped. One
# check, in this order:
#   1. a 401 or 403 in the listener's journal of the last 10 minutes: "credentials", and no restart,
#      since a restart cannot grant a permission;
#   2. the unit active and its newest journal line at most 3 minutes old: "ok";
#   3. otherwise one restart, unless the last one was under 30 minutes ago (the cooldown file holds
#      its epoch), so a lane with a real problem is not restart-looped; then a re-check 30 s later.
# A restart is safe with slots running: the listener leaves them alone when it stops and adopts
# them when it starts (listener/README.md, "Crash safety").
#
# The verdict is one line, "ok <utc>" or "unhealthy <utc> <reason>", in /run/<name>-listener-health
# (0644), with the cooldown beside it in /run/<name>-listener-health.restarted. Both sit beside the
# lane's run dir, not in it: /run/<name> is root's alone (0700).
#
# Linux-only by design (systemd, journald); it names no lane or machine: the lane is the argument.
set -euo pipefail

# Overridable only so the hermetic test can run unprivileged and without waiting.
HEALTH_DIR="${KNOWN_CI_LISTENER_HEALTH_DIR:-/run}"
SETTLE_SEC="${KNOWN_CI_LISTENER_HEALTH_SETTLE_SEC:-30}"
JOURNAL_MAX_AGE_SEC=180
COOLDOWN_SEC=1800
# A GitHub refusal as the listener logs one: its own client's "HTTP 403: ...", and the scaleset
# client's "unexpected status code: 403", "unexpected status code 403 Forbidden" and
# status="403 Forbidden". Never a bare 401 or 403: runner names end in an epoch that can hold one.
AUTH_RE='HTTP 40[13]([^0-9]|$)|status code:? 40[13]([^0-9]|$)|40[13] (Unauthorized|Forbidden)'

die() { printf 'listener-health: %s\n' "$*" >&2; exit 2; }

usage() {
  echo "usage: $0 <lane name>   check <name>-listener.service, restart it once when wedged, write the verdict"
}

# Seconds since the listener's newest journal line; empty when it has none.
journal_age() {
  local stamp
  stamp="$(journalctl -u "$UNIT" -n 1 -o short-unix --no-pager -q 2>/dev/null | awk 'NR == 1 { print $1 }' || true)"
  stamp="${stamp%%.*}"
  if [[ "$stamp" =~ ^[0-9]+$ ]]; then echo $(( $(date +%s) - stamp )); fi
}

# Why the listener is not serving; nothing when it is active and its journal is fresh.
wedged_reason() {
  local state age
  state="$(systemctl is-active "$UNIT" 2>/dev/null || true)"
  if [ "$state" != active ]; then printf 'listener %s' "${state:-unknown}"; return; fi
  age="$(journal_age)"
  if [ -z "$age" ]; then printf 'journal empty'; return; fi
  if [ "$age" -gt "$JOURNAL_MAX_AGE_SEC" ]; then printf 'journal silent for %ss' "$age"; fi
}

# ok | unhealthy <reason>: writes the verdict line (renamed into place, so a reader never sees half
# of one) and logs it.
verdict() {
  local line
  line="$1 $(date -u +%Y-%m-%dT%H:%M:%SZ)${2:+ $2}"
  printf '%s\n' "$line" > "$STATE.tmp"
  chmod 0644 "$STATE.tmp"
  mv -f "$STATE.tmp" "$STATE"
  printf '%s: %s\n' "$UNIT" "$line"
}

check_lane() {
  local auth reason now last
  auth="$(journalctl -u "$UNIT" --since "10 min ago" -o cat --no-pager -q 2>/dev/null | grep -cE "$AUTH_RE" || true)"
  if [ "${auth:-0}" -gt 0 ]; then
    verdict unhealthy "credentials: $auth GitHub 401/403 lines in 10 minutes (not restarted: a restart cannot grant a permission)"
    return 1
  fi
  reason="$(wedged_reason)"
  if [ -z "$reason" ]; then verdict ok; return 0; fi
  now="$(date +%s)"
  last="$(cat "$COOLDOWN" 2>/dev/null || true)"
  if [[ "$last" =~ ^[0-9]+$ ]] && [ $(( now - last )) -lt "$COOLDOWN_SEC" ]; then
    verdict unhealthy "$reason; not restarted: the last restart was $(( now - last ))s ago (one per ${COOLDOWN_SEC}s)"
    return 1
  fi
  printf '%s: %s, restarting it once\n' "$UNIT" "$reason"
  printf '%s\n' "$now" > "$COOLDOWN"
  if ! systemctl restart "$UNIT"; then
    verdict unhealthy "$reason; the restart failed"
    return 1
  fi
  sleep "$SETTLE_SEC"
  reason="$(wedged_reason)"
  if [ -n "$reason" ]; then
    verdict unhealthy "$reason after a restart"
    return 1
  fi
  verdict ok
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") usage >&2; exit 2 ;;
esac
[ "$#" -eq 1 ] || { usage >&2; exit 2; }
[[ "$1" =~ ^[a-z][a-z0-9-]*$ ]] || die "invalid lane name '$1' (allowed: [a-z][a-z0-9-]*)"
UNIT="$1-listener.service"
STATE="$HEALTH_DIR/$1-listener-health"
COOLDOWN="$STATE.restarted"
check_lane
