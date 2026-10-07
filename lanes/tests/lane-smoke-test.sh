#!/bin/bash
# lane-smoke-test.sh: hermetic tests for bin/lane-smoke.sh, the in-container proofs of
# bin/provision-lane.sh --check. Its in-container tools are stubs on PATH (no address answers, no
# write lands, dockerd and docker return at once); only its preload proof and its shape are judged
# here (provision-lane-test.sh covers how --check runs and reads it). It writes its own scratch
# files under /tmp, as it does in the container; those it created are removed after.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SMOKE="$ROOT/bin/lane-smoke.sh"
TMP="$(mktemp -d)"
scratch_before=""
for f in /tmp/dockerd.log /tmp/lane-pulls; do [ -e "$f" ] && scratch_before+="$f "; done
cleanup() {
  for f in /tmp/dockerd.log /tmp/lane-pulls; do case " $scratch_before " in *" $f "*) ;; *) rm -f "$f" ;; esac; done
  rm -rf "$TMP"
}
trap cleanup EXIT
pass=0; fail=0
expect() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "✓ $2"; else fail=$((fail+1)); echo "✗ $2"; fi; }

SM="$TMP/smoke"; mkdir -p "$SM/bin" "$SM/preload"
for tool in timeout getent setpriv mount supabase; do printf '#!/bin/bash\nexit 1\n' > "$SM/bin/$tool"; done
for tool in chown sleep dockerd; do printf '#!/bin/bash\nexit 0\n' > "$SM/bin/$tool"; done
printf '#!/bin/bash\necho 4\n' > "$SM/bin/nproc"
printf '#!/bin/bash\ncase "$1" in info) echo "29.5.2 overlay2" ;; esac\nexit 0\n' > "$SM/bin/docker"
chmod +x "$SM/bin/"*
run_smoke() { env PATH="$SM/bin:$PATH" KNOWN_CI_SMOKE_PRELOAD="$SM/preload" KNOWN_CI_SMOKE_MANIFEST="$SM/manifest" bash "$SMOKE" "$@" 2>/dev/null; }

: > "$SM/manifest"
out="$(run_smoke - - - 22)"
grep -q "^probe inner-docker ok " <<< "$out" && grep -qx "probe inner-pulls ok nothing to pull: the image preloads no project (empty $SM/manifest)" <<< "$out"
expect $? "the smoke passes inner-pulls for an image whose build preloaded nothing (an empty manifest)"
rm -f "$SM/manifest"
out="$(run_smoke - - - 22)"
grep -qx "probe inner-pulls fail no preloaded project under $SM/preload/<name>/supabase/config.toml" <<< "$out"; expect $? "the smoke fails inner-pulls for an image with no project and no manifest"
printf 'public.ecr.aws/supabase/postgres:17.6.1\n' > "$SM/manifest"
out="$(run_smoke - - - 22)"
grep -q "^probe inner-pulls fail no preloaded project" <<< "$out"; expect $? "the smoke fails inner-pulls for an image whose manifest lists images but whose projects are gone"
out="$(run_smoke 172.20.0.1 203.0.113.7 - "22 3101")"
grep -qx "probe net-gateway-22 ok 172.20.0.1:22 unreachable" <<< "$out" && grep -qx "probe net-public-3101 ok 203.0.113.7:3101 unreachable" <<< "$out" && grep -qx "probe net-tailnet skip no address on the host" <<< "$out"
expect $? "the smoke probes every declared port on every address the host has, and skips an address it lacks"
grep -q "^probe write-etc ok refuses uid 1001" <<< "$out" && grep -q "^probe write-home/runner fail" <<< "$out"; expect $? "every result is one probe line the provisioner parses"
# A job's own tests may shell out to psql: the smoke reports the image's, and fails one that does not run.
printf '#!/bin/bash\necho "psql (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)"\n' > "$SM/bin/psql"; chmod +x "$SM/bin/psql"
out="$(run_smoke - - - 22)"
grep -qx "probe psql ok psql (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)" <<< "$out"; expect $? "the smoke reports the psql the image carries"
printf '#!/bin/bash\nexit 127\n' > "$SM/bin/psql"
out="$(run_smoke - - - 22)"
grep -qx "probe psql fail psql is not on the image's PATH or does not run" <<< "$out"; expect $? "the smoke fails psql when the image's psql does not run"
rm -f "$SM/bin/psql"
out="$(env PATH="$SM/bin:$PATH" bash "$SMOKE" - - - 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && grep -q "the host ports to probe" <<< "$out"; expect $? "the smoke refuses to run without the lane's check_ports"
# The smoke bypasses the entrypoint, whose install_runner chowns the home to the job's uid; the smoke
# must do the same before probing writes as that uid, or /home/runner fails for the wrong reason.
smoke_chown="$(grep -n '^chown 1001:1001 /home/runner$' "$SMOKE" | cut -d: -f1)"
smoke_home_loop="$(grep -n '^for dir in /home/runner /tmp; do$' "$SMOKE" | cut -d: -f1)"
[ -n "$smoke_chown" ] && [ -n "$smoke_home_loop" ] && [ "$smoke_chown" -lt "$smoke_home_loop" ]; expect $? "the smoke chowns /home/runner to the job uid (as the entrypoint does) before the loop that probes writes there as that uid"
grep -qx 'export SUPABASE_INTERNAL_IMAGE_REGISTRY="${SUPABASE_INTERNAL_IMAGE_REGISTRY:-ghcr.io}"' "$SMOKE" && grep -qE '^ +export SUPABASE_INTERNAL_IMAGE_REGISTRY="\$\{SUPABASE_INTERNAL_IMAGE_REGISTRY:-ghcr.io\}"$' "$ROOT/image/entrypoint.sh"; expect $? "the smoke and the preload start Supabase against the registry supabase/setup-cli gives a job (ghcr.io), so the zero-pull proof counts the pulls a job would make"
grep -qx 'PORTS="${4:?the host ports to probe: the check_ports of the lane}"' "$SMOKE" && ! grep -q '54321' "$SMOKE"; expect $? "the smoke has no port list of its own: it probes the ports the lane declares"

echo
echo "lane-smoke-test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
