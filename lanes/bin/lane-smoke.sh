#!/usr/bin/env bash
# lane-smoke.sh: a lane's in-container proofs.
#
# It never runs on the host. bin/provision-lane.sh --check feeds it on standard input to `bash -s`
# inside a throwaway container started with the lane's own docker run flags (Sysbox runtime, the
# lane's network, the tmpfs /tmp, the slice), as that container's root:
#   docker run -i ... --entrypoint /bin/bash <image> -s -- <gateway> <public-ip> <tailnet-ip> <ports> < this
# so its environment is the runner image: Ubuntu 24.04, bash 5, coreutils, getent, dockerd, the
# Supabase CLI. Every result is one line, `probe <name> <ok|fail|skip> <detail>`, which the
# provisioner parses; a missing probe counts as a failure there.
#
# Proves, from inside a slot-shaped container:
#   - the bridge gateway, the host's public and tailnet addresses (on <ports>) and the
#     cloud metadata address are unreachable, while github.com resolves and answers on 443;
#   - the job's uid (1001) cannot write outside /home/runner and /tmp, sees 4 CPUs, and can execute from /tmp;
#   - psql runs (the image's PostgreSQL client);
#   - an inner dockerd starts, and `supabase start` for every preloaded project runs with zero
#     image pulls (counted from the inner `docker events`). An image whose preload map lists none
#     has no project to start; the build records that as an empty manifest, which passes, while a
#     missing or non-empty manifest with no project fails (the image lost its projects).
set -uo pipefail

GATEWAY="${1:-}"
PUBLIC="${2:-}"
TAILNET="${3:-}"
# The lane's check_ports (hosts/<host>.yml), space-separated: the host ports a slot must not reach.
PORTS="${4:?the host ports to probe: the check_ports of the lane}"
SUPABASE_EXCLUDE="studio,imgproxy,mailpit,edge-runtime,logflare,vector"
# The registry supabase/setup-cli exports for a job's steps; the preload fills the store with these
# tags, and a proof on the CLI's default registry would count pulls a job never makes (2026-09-28).
export SUPABASE_INTERNAL_IMAGE_REGISTRY="${SUPABASE_INTERNAL_IMAGE_REGISTRY:-ghcr.io}"
PRELOAD_ROOT="${KNOWN_CI_SMOKE_PRELOAD:-/opt/known-ci/preload}"
MANIFEST="${KNOWN_CI_SMOKE_MANIFEST:-/etc/known-ci-runner/preloaded-images}"

say() { printf 'probe %s %s %s\n' "$1" "$2" "${3:-}"; }
reachable() { timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

say container-ready ok "$(date +%s)"

# --- network --------------------------------------------------------------------------------------
for target in "gateway=$GATEWAY" "public=$PUBLIC" "tailnet=$TAILNET"; do
  label="${target%%=*}"
  host="${target#*=}"
  if [ -z "$host" ] || [ "$host" = - ]; then
    say "net-$label" skip "no address on the host"
    continue
  fi
  for port in $PORTS; do
    if reachable "$host" "$port"; then
      say "net-$label-$port" fail "$host:$port answered"
    else
      say "net-$label-$port" ok "$host:$port unreachable"
    fi
  done
done
if reachable 169.254.169.254 80; then
  say net-metadata fail "169.254.169.254:80 answered"
else
  say net-metadata ok "169.254.169.254:80 unreachable"
fi
if getent hosts github.com >/dev/null 2>&1 && reachable github.com 443; then
  say net-github ok "github.com resolves and answers on 443"
else
  say net-github fail "github.com did not resolve or did not answer on 443"
fi

# --- writes ---------------------------------------------------------------------------------------
# As the job's own uid (1001, no sudo in the image): the rootfs is writable for root because Sysbox
# mounts its /var/lib/docker read-only under --read-only and the inner dockerd cannot start, so the
# invariant is that the JOB cannot write outside /home/runner and /tmp.
as_job() { setpriv --reuid=1001 --regid=1001 --clear-groups -- "$@"; }
# The entrypoint (install_runner, right after require_fresh_home) chowns the mounted home to the job's uid;
# this probe bypasses the entrypoint, so it does the same, or the home stays root's and the probe
# fails for the wrong reason (measured 2026-09-26 on both a tmpfs and a real slot mount).
chown 1001:1001 /home/runner
for dir in /etc /usr /opt /var /root; do
  if as_job touch "$dir/.lane-smoke" 2>/dev/null; then
    rm -f "$dir/.lane-smoke"
    say "write-${dir#/}" fail "uid 1001 could write at $dir"
  else
    say "write-${dir#/}" ok "refuses uid 1001"
  fi
done
for dir in /home/runner /tmp; do
  if as_job touch "$dir/.lane-smoke" 2>/dev/null; then
    rm -f "$dir/.lane-smoke"
    say "write-${dir#/}" ok "writable"
  else
    say "write-${dir#/}" fail "a job could not write to $dir"
  fi
done

# --- hosted-runner parity: the CPU count a job sees, and an executable /tmp ----------------------
# Without a cpuset every container reports the host's cores and a jest run started 15 workers in a
# 4 GB container; Docker's tmpfs default is noexec and a job's stub scripts in /tmp failed (2026-09-28).
if [ "$(nproc)" = "${KNOWN_CI_CPUS:-4}" ]; then
  say cpus ok "nproc=$(nproc)"
else
  say cpus fail "nproc=$(nproc), expected ${KNOWN_CI_CPUS:-4}"
fi
printf '#!/bin/sh\necho ok\n' > /tmp/.lane-exec && chmod 0755 /tmp/.lane-exec
if [ "$(as_job /tmp/.lane-exec 2>/dev/null)" = ok ]; then
  say tmp-exec ok "a job can execute from /tmp"
else
  say tmp-exec fail "a job cannot execute from /tmp ($(findmnt -no OPTIONS /tmp 2>/dev/null))"
fi
rm -f /tmp/.lane-exec
# A job's own tests may shell out to psql; the image installs noble's postgresql-client.
if version="$(psql --version 2>/dev/null)"; then
  say psql ok "$version"
else
  say psql fail "psql is not on the image's PATH or does not run"
fi

# --- inner Docker and the preloaded Supabase images -----------------------------------------------
# A read-only root leaves /run read-only; Sysbox's root may mount its own tmpfs there.
if ! { touch /run/.lane-smoke && rm -f /run/.lane-smoke; } 2>/dev/null; then
  mount -t tmpfs -o size=64m,mode=0755 tmpfs /run 2>/dev/null || true
fi
dockerd >/tmp/dockerd.log 2>&1 &
ready=0
for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  say inner-docker fail "dockerd did not answer within 60s: $(tail -n 3 /tmp/dockerd.log | tr '\n' ' ' | cut -c1-300)"
  exit 0
fi
say inner-docker ok "$(docker info --format '{{.ServerVersion}} {{.Driver}}' 2>/dev/null)"

since="$(date +%s)"
docker events --since "$since" --filter type=image --filter event=pull --format '{{.Actor.ID}}' \
  > /tmp/lane-pulls 2>/dev/null &
events=$!
projects=0
for project in "$PRELOAD_ROOT"/*/; do
  [ -f "$project/supabase/config.toml" ] || continue
  projects=$((projects + 1))
  name="$(basename "$project")"
  started="$(date +%s)"
  # --ignore-health-check as in the preload: the proof is zero pulls, not a healthy stack (a project's
  # API schema list may name a schema only its migrations create, so PostgREST answers 503 there).
  if (cd "$project" && timeout 600 supabase start -x "$SUPABASE_EXCLUDE" --ignore-health-check) >"/tmp/supabase-$name.log" 2>&1; then
    say "supabase-$name" ok "started in $(( $(date +%s) - started ))s"
  else
    say "supabase-$name" fail "$(tail -n 3 "/tmp/supabase-$name.log" | tr '\n' ' ' | cut -c1-300)"
  fi
  (cd "$project" && timeout 120 supabase stop --no-backup) >/dev/null 2>&1 || true
done
sleep 2
kill "$events" 2>/dev/null
wait "$events" 2>/dev/null
if [ "$projects" = 0 ] && [ -f "$MANIFEST" ] && [ ! -s "$MANIFEST" ]; then
  say inner-pulls ok "nothing to pull: the image preloads no project (empty $MANIFEST)"
elif [ "$projects" = 0 ]; then
  say inner-pulls fail "no preloaded project under $PRELOAD_ROOT/<name>/supabase/config.toml"
else
  pulls="$(grep -c . /tmp/lane-pulls 2>/dev/null || true)"
  if [ "${pulls:-0}" = 0 ]; then
    say inner-pulls ok "0 image pulls across $projects preloaded project(s)"
  else
    say inner-pulls fail "${pulls} image pull(s): $(sort -u /tmp/lane-pulls | tr '\n' ' ' | cut -c1-300)"
  fi
fi
