#!/bin/bash
# runners-pull-test.sh: hermetic tests for bin/runners-pull.sh against two real local git origins:
# the engine's (a stand-in catalog carrying this kit under lanes/) and the values repository's (a
# host file under hosts/), each a repository on disk that a commit lands in as a merge would. The
# engine's provision-host.sh, provision-lane.sh and provision-dashboards.sh are stand-ins that
# record their calls; the lane list comes from the real lib/lanes.py on the values checkout's host
# file. git on PATH records each fetch's checkout and ssh command, then runs the real git. No ssh,
# network or machine state is touched.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }
gitc() { git -c user.name=t -c user.email=t@example.invalid -c init.defaultBranch=main "$@"; }

setup() {
  S="$TMP/s"
  rm -rf "$S"
  mkdir -p "$S/engine-seed/lanes/bin" "$S/engine-seed/lanes/lib" "$S/values-seed/hosts" "$S/etc" "$S/pathbin"
  cp "$ROOT/bin/runners-pull.sh" "$S/engine-seed/lanes/bin/"
  cp "$ROOT"/lib/*.py "$S/engine-seed/lanes/lib/"
  cp "$ROOT/examples/hosts/example.yml" "$S/values-seed/hosts/example.yml"
  for script in provision-host provision-lane provision-dashboards; do
    cat > "$S/engine-seed/lanes/bin/$script.sh" <<'SH'
#!/bin/bash
name="$(basename "$0" .sh)"
echo "$name $* engine=$(git -C "$(dirname "$0")/../.." rev-parse --short HEAD) values=$(git -C "$RUNNERS_CONFIG" rev-parse --short HEAD)" >> "$CALLLOG"
case " ${FAIL_ON:-} " in *" $name "*|*" $name:${2:-} "*) exit 1 ;; esac
exit 0
SH
    chmod +x "$S/engine-seed/lanes/bin/$script.sh"
  done
  printf '__pycache__/\n' > "$S/engine-seed/.gitignore"
  for repo in engine values; do
    gitc init -q "$S/$repo-seed" && gitc -C "$S/$repo-seed" add -A && gitc -C "$S/$repo-seed" commit -q -m one
  done
  # The machine's two checkouts: clones whose origin/main is each origin repository's main.
  git clone -q "$S/engine-seed" "$S/engine"
  git clone -q "$S/values-seed" "$S/config"
  # Records each fetch's checkout and ssh command, then runs the git that follows it on PATH.
  cat > "$S/pathbin/git" <<SH
#!/bin/bash
if [ "\${1:-}" = -C ] && [ "\${3:-}" = fetch ]; then echo "fetch \$2 ssh=\${GIT_SSH_COMMAND:-none}" >> "$S/fetch.log"; fi
PATH="\${PATH#$S/pathbin:}" exec git "\$@"
SH
  chmod +x "$S/pathbin/git"
  printf 'example\n' > "$S/etc/runners-host"
  printf 'not a real key\n' > "$S/etc/deploy_key"
  CALLLOG="$S/calls.log"; : > "$CALLLOG"; : > "$S/fetch.log"
  export CALLLOG
}

run_pull() {
  env PATH="$S/pathbin:$PATH" RUNNERS_ENGINE="$S/engine" RUNNERS_CONFIG="$S/config" RUNNERS_HOST_FILE="$S/etc/runners-host" \
    RUNNERS_DEPLOY_KEY="$S/etc/deploy_key" RUNNERS_PULL_STATE_DIR="$S/state" RUNNERS_PULL_LOCK="$S/pull.lock" PYTHONDONTWRITEBYTECODE=1 \
    bash "$S/engine/lanes/bin/runners-pull.sh" > "$S/out" 2>&1
}
# A new commit on a repository's main, as a merge would land one:
#   land values <message>    the values repository moves (any commit there is one to apply)
#   land kit <message>       the catalog changes a file under lanes/, so the kit's tree moves
#   land catalog <message>   the catalog changes a file outside lanes/: the engine's commit moves and
#                            the kit's tree does not
land() {
  local file
  case "$1" in
    values) gitc -C "$S/values-seed" commit -q --allow-empty -m "$2"; return ;;
    kit) file="lanes/CHANGES" ;;
    catalog) file="docs/CHANGES" ;;
  esac
  mkdir -p "$S/engine-seed/$(dirname "$file")" && printf '%s\n' "$2" >> "$S/engine-seed/$file" \
    && gitc -C "$S/engine-seed" add "$file" && gitc -C "$S/engine-seed" commit -q -m "$2"
}
# The values repository's host file without the acme-ci lane and its dashboard.
drop_acme_lane() {
  python3 -c '
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
doc["lanes"] = [lane for lane in doc["lanes"] if lane["name"] != "acme-ci"]
doc["dashboards"] = [d for d in doc["dashboards"] if d["lane"] != "acme-ci"]
yaml.safe_dump(doc, open(sys.argv[1], "w"), sort_keys=False)
' "$S/values-seed/hosts/example.yml"
}
applied() { cat "$S/state/applied" 2>/dev/null; }
engine_head() { git -C "$S/engine" rev-parse HEAD; }
kit_tree() { git -C "$S/engine" rev-parse HEAD:lanes; }
config_head() { git -C "$S/config" rev-parse HEAD; }
heads() { printf '%s %s' "$(engine_head)" "$(config_head)"; }
# What a successful apply records: the kit's tree, not the engine's commit, and the values commit.
record() { printf 'engine=%s config=%s' "$(kit_tree)" "$(config_head)"; }
origin_of() { git -C "$S/$1-seed" rev-parse main; }
calls() { wc -l < "$CALLLOG" | tr -d ' '; }

# ---- a first run: nothing recorded, so the machine converges ---------------------------------------
setup
run_pull; rc=$?
[ "$rc" -eq 0 ]; expect $? "a first run exits 0"
[ "$(sed -n 1p "$CALLLOG" | cut -d' ' -f1-3)" = "provision-host example --apply" ] && [ "$(sed -n 2p "$CALLLOG" | cut -d' ' -f1-4)" = "provision-lane example greenbauer-ci --apply" ] \
  && [ "$(sed -n 3p "$CALLLOG" | cut -d' ' -f1-4)" = "provision-lane example acme-ci --apply" ] \
  && [ "$(sed -n 4p "$CALLLOG" | cut -d' ' -f1-3)" = "provision-dashboards example --apply" ] && [ "$(calls)" = 4 ]
expect $? "with no apply recorded it runs provision-host, provision-lane for each lane of the values checkout's hosts/<host>.yml, then provision-dashboards, in order"
[ "$(applied)" = "$(record)" ] && [ "$(applied)" != "engine=$(engine_head) config=$(config_head)" ] \
  && [ "$(git -C "$S/engine" cat-file -t "$(kit_tree)")" = tree ]
expect $? "the success is recorded under the state directory as the tree of lanes/ in the engine checkout, not the engine's commit, and the values checkout's HEAD"

# ---- the engine is fetched with no credential, the values checkout with the deploy key --------------
[ "$(grep -c "^fetch $S/engine ssh=none$" "$S/fetch.log")" = 1 ]; expect $? "the engine checkout is fetched with no ssh command or key: it is public"
[ "$(grep -cxF "fetch $S/config ssh=ssh -i $S/etc/deploy_key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new" "$S/fetch.log")" = 1 ] && [ "$(wc -l < "$S/fetch.log" | tr -d ' ')" = 2 ]
expect $? "the values checkout is fetched with the deploy key alone (IdentitiesOnly, BatchMode), and nothing else is fetched"

# ---- no change, no apply ---------------------------------------------------------------------------
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ ! -s "$CALLLOG" ]; expect $? "with no new commit in either repository and the pair applied, a tick applies nothing"

# ---- a catalog merge that touches nothing under lanes/: fast-forward, and nothing to apply ----------
land catalog "a gate fix"
: > "$CALLLOG"
before="$(engine_head)"; kit="$(kit_tree)"; record_before="$(applied)"
run_pull; rc=$?
after="$(engine_head)"
[ "$rc" -eq 0 ] && [ "$after" != "$before" ] && [ "$after" = "$(origin_of engine)" ] && [ "$(kit_tree)" = "$kit" ]
expect $? "a catalog commit outside lanes/ is fetched and fast-forwarded to: the engine's commit moves and the kit's tree does not"
[ ! -s "$CALLLOG" ] && [ "$(applied)" = "$record_before" ] && ! grep -q "converging\|applied" "$S/out"
expect $? "it applies nothing and leaves the record untouched"
land catalog "another"; land catalog "and another"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ "$(engine_head)" = "$(origin_of engine)" ] && [ ! -s "$CALLLOG" ] && [ "$(applied)" = "$record_before" ]; expect $? "however many such commits land, the machine stays converged"

# ---- a merge lands under lanes/: fast-forward, then converge ---------------------------------------
land kit "a kit fix"
: > "$CALLLOG"
before="$(engine_head)"; kit="$(kit_tree)"; values_before="$(config_head)"
run_pull; rc=$?
after="$(engine_head)"; now="$(kit_tree)"
[ "$rc" -eq 0 ] && [ "$after" != "$before" ] && [ "$after" = "$(origin_of engine)" ] && [ "$now" != "$kit" ] && [ "$(config_head)" = "$values_before" ]
expect $? "a catalog commit under lanes/ is fetched and fast-forwarded to: the kit's tree moves, and the values checkout stays where it was"
[ "$(calls)" = 4 ] && grep -q "^provision-host example --apply engine=$(git -C "$S/engine" rev-parse --short HEAD) values=$(git -C "$S/config" rev-parse --short HEAD)$" "$CALLLOG"; expect $? "when only the kit moved it applies: host, each lane, then the dashboards, from the updated engine"
[ "$(applied)" = "$(record)" ] && grep -qF "engine ${before:0:12} -> ${after:0:12} (kit tree ${kit:0:12} -> ${now:0:12}), values ${values_before:0:12} -> ${values_before:0:12}; converging example" "$S/out" \
  && grep -qF "applied engine ${after:0:12} (kit tree ${now:0:12}) and values ${values_before:0:12} to example" "$S/out"
expect $? "the new kit tree is recorded beside the values HEAD, and the journal names the engine's commit range"
land catalog "after the kit fix"; land kit "a second kit fix"
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ "$(calls)" = 4 ] && [ "$(applied)" = "$(record)" ]; expect $? "commits in and outside lanes/ landing together apply once"

# ---- a merge lands in the values repository alone ---------------------------------------------------
land values two
: > "$CALLLOG"
before="$(config_head)"; engine_before="$(engine_head)"
run_pull; rc=$?
after="$(config_head)"
[ "$rc" -eq 0 ] && [ "$after" != "$before" ] && [ "$after" = "$(origin_of values)" ] && [ "$(engine_head)" = "$engine_before" ]; expect $? "a new commit on the values repository's origin/main is fetched and fast-forwarded to, and the engine stays where it was"
[ "$(calls)" = 4 ] && grep -q "^provision-dashboards example --apply engine=$(git -C "$S/engine" rev-parse --short HEAD) values=$(git -C "$S/config" rev-parse --short HEAD)$" "$CALLLOG" && [ "$(applied)" = "$(record)" ]
expect $? "when only the values HEAD moved it applies the machine again and records the pair"
# The host file is the values checkout's: a lane dropped there is no longer applied.
drop_acme_lane
git -C "$S/values-seed" add hosts/example.yml && land values "drop a lane"
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ "$(calls)" = 3 ] && grep -q "^provision-lane example greenbauer-ci --apply" "$CALLLOG" && ! grep -q "acme-ci" "$CALLLOG" && [ "$(applied)" = "$(record)" ]
expect $? "the lanes it applies are those of the host file the values checkout now holds"

# ---- the kit and the values move in one tick: one apply ---------------------------------------------
setup
run_pull
land kit both; land values both
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ "$(engine_head)" = "$(origin_of engine)" ] && [ "$(config_head)" = "$(origin_of values)" ] && [ "$(calls)" = 4 ] && [ "$(applied)" = "$(record)" ]
expect $? "commits in both repositories are fast-forwarded to and applied once, together"

# ---- a failed apply is not recorded, so the next tick retries ----------------------------------
land kit three
: > "$CALLLOG"
FAIL_ON="provision-lane:greenbauer-ci" run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -qF -- "--apply failed for provision-lane.sh:greenbauer-ci; nothing is recorded" "$S/out" && [ "$(applied)" != "$(record)" ]
expect $? "a failed lane apply exits non-zero and records nothing"
grep -q "^provision-lane example acme-ci --apply" "$CALLLOG" && grep -q "^provision-dashboards example --apply" "$CALLLOG"; expect $? "a failed lane stops neither the machine's other lanes nor its dashboards"
land catalog "while the apply is failing"
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ "$(calls)" = 4 ] && [ "$(applied)" = "$(record)" ]; expect $? "the next tick retries the same kit and values, though its only new commit is outside lanes/, and records them once it succeeds"
land values three-b
: > "$CALLLOG"
FAIL_ON="provision-dashboards" run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -qF -- "--apply failed for provision-dashboards.sh; nothing is recorded" "$S/out" && [ "$(applied)" != "$(record)" ] && [ "$(calls)" = 4 ]
expect $? "a failed dashboards apply exits non-zero and records nothing, after the host and every lane were applied"
land kit four
: > "$CALLLOG"
FAIL_ON="provision-host" run_pull; rc=$?
[ "$rc" -ne 0 ] && [ "$(calls)" = 1 ] && grep -q "the lanes were not applied and nothing is recorded" "$S/out" && [ "$(applied)" != "$(record)" ]
expect $? "a failed host apply skips the lanes and records nothing"

# ---- a record from before the values checkout existed ---------------------------------------------
setup
run_pull
engine_head > "$S/state/applied"
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ "$(calls)" = 4 ] && [ "$(applied)" = "$(record)" ]; expect $? "a record of one HEAD alone, as an earlier single-checkout pull left it, matches nothing: one apply, then the pair is recorded"

# ---- a change to runners-pull.sh itself applies on the tick that pulls it ---------------------------
setup
run_pull
sed -i 's/log "applied engine ${engine_head:0:12} (kit tree ${kit_tree:0:12}) and values ${config_head:0:12} to $host"/log "applied engine ${engine_head:0:12} (kit tree ${kit_tree:0:12}) and values ${config_head:0:12} to $host by the new copy"/' "$S/engine-seed/lanes/bin/runners-pull.sh"
grep -q "by the new copy" "$S/engine-seed/lanes/bin/runners-pull.sh" && gitc -C "$S/engine-seed" add lanes/bin/runners-pull.sh && gitc -C "$S/engine-seed" commit -q -m "new pull"
land values "beside the new pull"
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && grep -q "lanes/bin/runners-pull.sh changed in .*; running the new copy" "$S/out" && grep -q "applied engine .* and values .* to example by the new copy" "$S/out" \
  && [ "$(calls)" = 4 ] && [ "$(applied)" = "$(record)" ] && [ "$(config_head)" = "$(origin_of values)" ]
expect $? "a pulled change to runners-pull.sh runs the new copy in its place, which fast-forwards the values checkout, applies the pair once and records it"
land kit six
: > "$CALLLOG"
run_pull; rc=$?
[ "$rc" -eq 0 ] && ! grep -q "running the new copy" "$S/out" && [ "$(calls)" = 4 ]; expect $? "a pull that leaves runners-pull.sh as it was applies without re-running it"

# ---- local modifications are refused, in either checkout -------------------------------------------
setup
run_pull
land kit five; land values five
printf '# edited on the machine\n' >> "$S/config/hosts/example.yml"
: > "$CALLLOG"; : > "$S/fetch.log"; before="$(heads)"
run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -q "$S/config has local modifications; nothing fetched or applied" "$S/out" && grep -q "hosts/example.yml" "$S/out" && [ "$(heads)" = "$before" ] && [ ! -s "$CALLLOG" ] && [ ! -s "$S/fetch.log" ]
expect $? "a values checkout with a tracked file edited by hand is refused: neither checkout fetched, nothing applied"
git -C "$S/config" checkout -q -- hosts/example.yml
printf '# edited on the machine\n' >> "$S/engine/lanes/bin/provision-host.sh"
run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -q "$S/engine has local modifications; nothing fetched or applied" "$S/out" && grep -q "lanes/bin/provision-host.sh" "$S/out" && [ "$(heads)" = "$before" ] && [ ! -s "$CALLLOG" ] && [ ! -s "$S/fetch.log" ]
expect $? "an engine checkout with a tracked file edited by hand is refused: neither checkout fetched, nothing applied"
git -C "$S/engine" checkout -q -- lanes/bin/provision-host.sh
printf 'stray\n' > "$S/engine/lanes/bin/hand-added.sh"
run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -q "hand-added.sh" "$S/out" && [ ! -s "$CALLLOG" ]; expect $? "an untracked file added by hand to the engine is a local modification too"
rm "$S/engine/lanes/bin/hand-added.sh"
printf 'hostname: stray\n' > "$S/config/hosts/hand-added.yml"
run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -q "hand-added.yml" "$S/out" && [ ! -s "$CALLLOG" ]; expect $? "an untracked file added by hand to the values checkout is one as well"
rm "$S/config/hosts/hand-added.yml"
run_pull; rc=$?
[ "$rc" -eq 0 ] && [ "$(engine_head)" = "$(origin_of engine)" ] && [ "$(config_head)" = "$(origin_of values)" ] && [ "$(applied)" = "$(record)" ]; expect $? "once the edits are gone the next tick fast-forwards both checkouts and converges"

# ---- what it needs before it does anything --------------------------------------------------------
setup
rm "$S/etc/runners-host"
run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -q "must hold this machine's host config name" "$S/out" && [ ! -s "$CALLLOG" ]; expect $? "without /etc/runners-host it applies nothing and fails"
printf '../etc\n' > "$S/etc/runners-host"
run_pull; rc=$?
[ "$rc" -ne 0 ] && [ ! -s "$CALLLOG" ]; expect $? "a host name that is not a config name is refused"
printf 'example\n' > "$S/etc/runners-host"; rm "$S/etc/deploy_key"
run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -q "no deploy key at $S/etc/deploy_key" "$S/out" && [ ! -s "$CALLLOG" ]; expect $? "without the deploy key it applies nothing and fails, naming the key"
setup
mv "$S/config" "$S/config.aside"
run_pull; rc=$?
[ "$rc" -ne 0 ] && grep -q "$S/config is not a git checkout; nothing fetched or applied" "$S/out" && [ ! -s "$CALLLOG" ] && [ ! -s "$S/fetch.log" ] && [ -z "$(applied)" ]
expect $? "without the values checkout it fetches nothing, applies nothing and fails, naming where it belongs"
setup
exec 7>"$S/pull.lock"; flock -n 7
run_pull; rc=$?
exec 7>&-
[ "$rc" -eq 0 ] && grep -q "another run holds $S/pull.lock; this tick does nothing" "$S/out" && [ ! -s "$CALLLOG" ] && [ -z "$(applied)" ]; expect $? "a tick while another run holds the lock does nothing"
grep -qx 'ENGINE="${RUNNERS_ENGINE:-/opt/runner-lanes}"' "$ROOT/bin/runners-pull.sh" && grep -qx 'CONFIG="${RUNNERS_CONFIG:-/opt/runner-lanes-config}"' "$ROOT/bin/runners-pull.sh" \
  && grep -qx 'KIT="$ENGINE/lanes"' "$ROOT/bin/runners-pull.sh" && grep -qx 'DEPLOY_KEY="${RUNNERS_DEPLOY_KEY:-/etc/runners/deploy_key}"' "$ROOT/bin/runners-pull.sh" \
  && grep -qx 'HOST_FILE="${RUNNERS_HOST_FILE:-/etc/runners-host}"' "$ROOT/bin/runners-pull.sh" && grep -qx 'STATE_DIR="${RUNNERS_PULL_STATE_DIR:-/var/lib/runners}"' "$ROOT/bin/runners-pull.sh"
expect $? "on a machine the engine is /opt/runner-lanes with this kit under lanes/, the values checkout /opt/runner-lanes-config, the deploy key /etc/runners/deploy_key, the host name in /etc/runners-host and the record under /var/lib/runners"

echo
echo "runners-pull-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
