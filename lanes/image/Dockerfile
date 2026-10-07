# The one-job GitHub Actions runner image every lane builds under its own name.
#
# Target: linux/amd64 only. Built on the lane's machine (Ubuntu 24.04 amd64) by
# bin/build-runner-image.sh and run there under Sysbox (docker run --runtime=sysbox-runc ...).
# Every download below is pinned by version and sha256; README.md says where each pin came from and
# how to bump it. Changing any file in this directory except *.md changes the image tag.
#
# The inner Docker keeps Sysbox's supported layout: data-root /var/lib/docker (Sysbox backs it with
# its own host mount), the classic overlay2 store (daemon.json), and the pid/socket under /run.

ARG UBUNTU=ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

# ---- fetch: download and verify every pinned artifact; nothing from this stage but /out ships ----
FROM --platform=linux/amd64 ${UBUNTU} AS fetch
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

ARG RUNNER_VERSION=2.337.0
ARG RUNNER_SHA256=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613
ARG NODE_VERSION=22.23.3
ARG NODE_SHA256=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
ARG BUN_VERSION=1.4.2
ARG BUN_SHA256=36368faef7527875d5ffa52e53cd48021741f2a83eb6208a8dd64068d422a913
ARG UV_VERSION=0.12.19
ARG UV_SHA256=23bf5552d220e0842b65c862097b2ebaeba0064b74eda5e565e77fd25969d8c8
ARG GH_VERSION=2.101.0
ARG GH_SHA256=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
ARG SUPABASE_VERSION=2.118.0
ARG SUPABASE_SHA256=f6089a86fb9d9221c958193a277338daddd6822f706929943812fa32e106c86d
ARG GITLEAKS_VERSION=8.30.1
ARG GITLEAKS_SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
# Docker's apt signing key (fingerprint 9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88).
ARG DOCKER_GPG_SHA256=1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip xz-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /fetch
# fetch <file> <url> <sha256>: download, then refuse anything that misses its pin.
RUN fetch() { curl -fsSL --retry 3 -o "$1" "$2"; echo "$3  $1" | sha256sum -c --strict -; }; \
    fetch runner.tgz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" "$RUNNER_SHA256"; \
    fetch node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" "$NODE_SHA256"; \
    fetch bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" "$BUN_SHA256"; \
    fetch uv.tgz "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" "$UV_SHA256"; \
    fetch gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" "$GH_SHA256"; \
    fetch supabase.tgz "https://github.com/supabase/cli/releases/download/v${SUPABASE_VERSION}/supabase_linux_amd64.tar.gz" "$SUPABASE_SHA256"; \
    fetch gitleaks.tgz "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" "$GITLEAKS_SHA256"; \
    fetch docker.asc "https://download.docker.com/linux/ubuntu/gpg" "$DOCKER_GPG_SHA256"; \
    install -d /out/opt/actions-runner /out/usr/local/bin /out/etc/apt/keyrings; \
    tar -xzf runner.tgz -C /out/opt/actions-runner --no-same-owner; \
    tar -xJf node.tar.xz -C /out/usr/local --strip-components=1 --no-same-owner; \
    unzip -q bun.zip; install -m 0755 bun-linux-x64/bun /out/usr/local/bin/bun; ln -s bun /out/usr/local/bin/bunx; \
    tar -xzf uv.tgz; install -m 0755 uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx /out/usr/local/bin/; \
    tar -xzf gh.tgz; install -m 0755 "gh_${GH_VERSION}_linux_amd64/bin/gh" /out/usr/local/bin/gh; \
    tar -xzf supabase.tgz supabase supabase-go; install -m 0755 supabase supabase-go /out/usr/local/bin/; \
    tar -xzf gitleaks.tgz gitleaks; install -m 0755 gitleaks /out/usr/local/bin/gitleaks; \
    install -m 0644 docker.asc /out/etc/apt/keyrings/docker.asc

# ---- the image ----
FROM --platform=linux/amd64 ${UBUNTU}
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

ARG DOCKER_VERSION=5:29.8.1-1~ubuntu.24.04~noble
ARG CONTAINERD_VERSION=2.3.6-1~ubuntu.24.04~noble
ARG PLAYWRIGHT_VERSION=1.58.1

# SUPABASE_HOME: the CLI appends traces and telemetry state there on every command, and the default
# ~/.supabase is read-only for root (the lane's smoke) under --read-only; /tmp is writable in
# every mode.
ENV LANG=C.UTF-8 SUPABASE_HOME=/tmp/supabase-home

COPY --from=fetch /out/ /

# The one apt layer: base tools, psql (noble's PostgreSQL 16 client: a job's own tests may shell
# out to it), the runner's .NET libraries (its installdependencies.sh list for noble),
# Python 3.12, the pinned Docker CE engine, and Playwright's Chromium OS packages, then clean.
RUN export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
      > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      curl git jq openssh-client procps rsync unzip xz-utils postgresql-client \
      libicu74 libkrb5-3 liblttng-ust1t64 libssl3t64 zlib1g \
      python3 python3-pip python3-venv \
      "docker-ce=${DOCKER_VERSION}" "docker-ce-cli=${DOCKER_VERSION}" "containerd.io=${CONTAINERD_VERSION}"; \
    npx --yes "playwright@${PLAYWRIGHT_VERSION}" install-deps chromium; \
    corepack enable pnpm; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache /tmp/*

# `runner` (uid 1001) runs the job; its only extra group is the INNER docker's.
RUN getent group docker >/dev/null || groupadd --system docker; \
    groupadd --gid 1001 runner; \
    useradd --uid 1001 --gid runner --groups docker --home-dir /home/runner --no-create-home --shell /bin/bash runner; \
    install -d -o runner -g runner -m 0755 /home/runner; \
    install -d -m 0755 /etc/known-ci-runner /opt/known-ci/preload

# A legacy label, kept for machines already running: between jobs nothing references this image,
# and a disk-watch job some of them still run prunes unreferenced images at 85% disk with
# `--filter label!=ai-fleet.disk-watch=keep`; docker commit keeps the label.
LABEL ai-fleet.disk-watch=keep

COPY daemon.json /etc/docker/daemon.json
COPY --chmod=0755 entrypoint.sh /usr/local/bin/known-ci-entrypoint

ENTRYPOINT ["/usr/local/bin/known-ci-entrypoint"]
