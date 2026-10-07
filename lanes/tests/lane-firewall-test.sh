#!/bin/bash
# lane-firewall-test.sh: hermetic tests for bin/lane-firewall.sh, the per-lane firewall assertion
# bin/provision-lane.sh runs at --apply and bin/lane-slot.sh before every job. Stubs nft and
# iptables on PATH and records every call; no real firewall is touched.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/bin/lane-firewall.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }

RANGES='{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 }'
INSERT="nft insert rule ip filter DOCKER-USER iifname \"box-ci0\" ip daddr $RANGES counter reject comment \"ai-fleet-known-ci\""
INPUT_INSERT='iptables -I INPUT 1 -i box-ci0 -m comment --comment ai-fleet-known-ci -j REJECT'

setup() {
  rm -rf "$TMP/s"; mkdir -p "$TMP/s/bin"
  CALLLOG="$TMP/s/calls.log"; : > "$CALLLOG"
  LISTING="$TMP/s/listing"; : > "$LISTING"
  export CALLLOG LISTING
  cat > "$TMP/s/bin/nft" <<'SH'
#!/bin/bash
if [ "$1" = list ]; then
  [ "${NFT_NO_CHAIN:-0}" = 1 ] && exit 1
  exit 0
fi
if [ "$1" = -a ] && [ "$2" = list ]; then
  cat "$LISTING"; exit 0
fi
echo "nft $*" >> "$CALLLOG"
[ "$1" = delete ] && exit "${NFT_DELETE_RC:-0}"
exit 0
SH
  cat > "$TMP/s/bin/iptables" <<'SH'
#!/bin/bash
echo "iptables $*" >> "$CALLLOG"
[ "$1" = -C ] && exit "${IPT_PRESENT_RC:-1}"
exit 0
SH
  chmod +x "$TMP/s/bin/"*
}

run() { env PATH="$TMP/s/bin:$PATH" KNOWN_CI_BRIDGE="${BRIDGE_UNDER_TEST-box-ci0}" bash "$SRC" "$@"; }

# ---- a first assertion -------------------------------------------------------------------------------
setup
out="$(run 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx "lane-firewall: rules asserted on box-ci0" <<< "$out"; expect $? "the assertion exits 0 and says which bridge it covered"
grep -qxF "$INSERT" "$CALLLOG"; expect $? "DOCKER-USER rejects the lane bridge to RFC1918, CGNAT and link-local"
grep -qxF "iptables -C INPUT -i box-ci0 -m comment --comment ai-fleet-known-ci -j REJECT" "$CALLLOG" && grep -qxF "$INPUT_INSERT" "$CALLLOG"; expect $? "INPUT gets the bridge's reject at position 1 through iptables"
{ ! grep -q 'ip6' "$CALLLOG"; }; expect $? "the lane rules are IPv4-only (the lane network has no IPv6)"
{ ! grep -q 'flush' "$CALLLOG"; }; expect $? "the assertion never flushes DOCKER-USER (running slots stay filtered)"

# ---- idempotence: a present INPUT rule is not duplicated, an old forward rule is replaced ---------
setup
printf '%s\n' 'iifname "box-ci0" ip daddr { 10.0.0.0/8 } counter packets 3 bytes 180 reject comment "ai-fleet-known-ci" # handle 17' > "$LISTING"
out="$(IPT_PRESENT_RC=0 run 2>&1)"; rc=$?
[ "$rc" -eq 0 ]; expect $? "a repeat assertion exits 0"
{ ! grep -q '^iptables -I' "$CALLLOG"; }; expect $? "a present INPUT rule is not inserted twice"
insert_line="$(grep -nF "$INSERT" "$CALLLOG" | cut -d: -f1)"
delete_line="$(grep -n '^nft delete rule ip filter DOCKER-USER handle 17$' "$CALLLOG" | cut -d: -f1)"
[ -n "$insert_line" ] && [ -n "$delete_line" ] && [ "$insert_line" -lt "$delete_line" ]; expect $? "the new forward rule goes in before the old copy is deleted"
[ "$(grep -c '^nft delete' "$CALLLOG" | tr -d ' ')" = 1 ]; expect $? "only the tagged rule is deleted"
setup
printf '%s\n' 'iifname "box-ci0" ip daddr { 10.0.0.0/8 } counter packets 3 bytes 180 reject comment "ai-fleet-known-ci" # handle 17' > "$LISTING"
out="$(NFT_DELETE_RC=1 run 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qxF "$INSERT" "$CALLLOG"; expect $? "a handle a concurrent slot already deleted does not fail the assertion"
setup
printf '%s\n' 'iifname "eth0" ct state new counter packets 0 bytes 0 drop # handle 4' > "$LISTING"
out="$(run 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ! grep -q '^nft delete' "$CALLLOG"; expect $? "an untagged rule is never deleted"

# ---- two lanes on one machine: each assertion replaces its own bridge's rule only ----------------
setup
printf '%s\n' \
  'iifname "box-ci0" ip daddr { 10.0.0.0/8 } counter packets 3 bytes 180 reject comment "ai-fleet-known-ci" # handle 17' \
  'iifname "own-ci0" ip daddr { 10.0.0.0/8 } counter packets 5 bytes 300 reject comment "ai-fleet-known-ci" # handle 21' > "$LISTING"
out="$(run 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -qx '^nft delete rule ip filter DOCKER-USER handle 17$' "$CALLLOG" && ! grep -q 'handle 21' "$CALLLOG"
expect $? "asserting box-ci0 never deletes another lane's tagged rule (own-ci0's stays, so its jobs stay filtered)"

# ---- fail closed ----------------------------------------------------------------------------------
setup
out="$(NFT_NO_CHAIN=1 run 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "the box-ci0 forward rule is NOT asserted" <<< "$out"; expect $? "the assertion fails when DOCKER-USER is absent"
grep -qxF "$INPUT_INSERT" "$CALLLOG"; expect $? "the INPUT reject is still asserted when the forward rule cannot be"
setup
out="$(BRIDGE_UNDER_TEST=lane9 run 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && grep -q 'iifname "lane9"' "$CALLLOG" && grep -q -- '-i lane9 ' "$CALLLOG"; expect $? "the bridge name is one variable for both rules"
setup
out="$(env -u KNOWN_CI_BRIDGE PATH="$TMP/s/bin:$PATH" bash "$SRC" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "set KNOWN_CI_BRIDGE" <<< "$out" && [ ! -s "$CALLLOG" ]; expect $? "without a bridge name it asserts nothing and fails (there is no default lane)"
out="$(run --docker-only 2>&1)"; rc=$?
[ "$rc" -eq 2 ] && [ ! -s "$CALLLOG" ]; expect $? "it takes no arguments"

echo
echo "lane-firewall-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
