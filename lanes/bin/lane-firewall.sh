#!/usr/bin/env bash
# lane-firewall.sh: assert one lane's two firewall rules, without flushing anything.
#
#   KNOWN_CI_BRIDGE=<name>0 lane-firewall.sh
#
# A lane (bin/provision-lane.sh) runs its jobs in Sysbox containers on its own Docker bridge,
# <name>0. Two rules keep a job off everything private:
#   - DOCKER-USER (FORWARDED traffic only): reject from the bridge to RFC1918, the CGNAT/tailnet
#     range and link-local (cloud metadata). That also covers a host's published ports, which
#     Docker DNATs to a container address and forwards.
#   - INPUT: reject everything from the bridge, because traffic to the host's own addresses (bridge
#     gateway, public IP, tailnet IP, docker-proxy) is delivered locally and never reaches DOCKER-USER.
# Both are inert on a machine without that bridge. The lane network is IPv4-only, so these are IPv4
# rules. The INPUT rule goes through iptables, not nft, so ufw (which drives INPUT with iptables)
# can always parse the chain it manages. DOCKER-USER is the opposite case: native nft rules there
# make iptables report the chain "incompatible, use 'nft'" (iptables 1.8.10), Docker only jumps to
# it from FORWARD, and so its lane rule is nft.
#
# Idempotent and safe while slots run: provision-lane.sh runs it at --apply, and bin/lane-slot.sh
# before every job, which is also what restores the rules after a reboot or a Docker restart
# (Docker re-creates DOCKER-USER empty). Exits non-zero, failing closed, when DOCKER-USER is absent.
# A legacy name, kept for machines already running: their rules carry this comment.
set -euo pipefail
BRIDGE="${KNOWN_CI_BRIDGE:?set KNOWN_CI_BRIDGE to the lane bridge (<name>0)}"
TAG="ai-fleet-known-ci"
PRIVATE="{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 }"

# Assert the DOCKER-USER reject without a window: insert the new rule first, then delete whatever
# copies were there before, so a job in a concurrent slot is never unfiltered.
docker_user_rule() {
  nft list chain ip filter DOCKER-USER >/dev/null 2>&1 || return 1
  local old h
  old="$(nft -a list chain ip filter DOCKER-USER 2>/dev/null \
    | awk -v tag="comment \"$TAG\"" -v bridge="iifname \"$BRIDGE\"" 'index($0, tag) && index($0, bridge) { print $NF }')"
  nft insert rule ip filter DOCKER-USER iifname "\"$BRIDGE\"" ip daddr "$PRIVATE" \
    counter reject comment "\"$TAG\""
  # Slots assert this concurrently. A handle another assertion already deleted is not an error;
  # the worst interleaving leaves two identical rules, which the next assertion collapses to one.
  for h in $old; do
    nft delete rule ip filter DOCKER-USER handle "$h" 2>/dev/null || true
  done
}

input_rule() {
  iptables -C INPUT -i "$BRIDGE" -m comment --comment "$TAG" -j REJECT 2>/dev/null \
    || iptables -I INPUT 1 -i "$BRIDGE" -m comment --comment "$TAG" -j REJECT
}

[ "$#" -eq 0 ] || { echo "usage: KNOWN_CI_BRIDGE=<name>0 $0" >&2; exit 2; }
input_rule
docker_user_rule || { echo "lane-firewall: DOCKER-USER chain absent (is docker up?); the $BRIDGE forward rule is NOT asserted" >&2; exit 1; }
echo "lane-firewall: rules asserted on $BRIDGE"
