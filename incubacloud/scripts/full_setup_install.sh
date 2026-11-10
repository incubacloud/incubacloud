#!/usr/bin/env bash
# Install the host toolchain for a full setup: git, Docker CE, Compose v2,
# the Python toolchain (pip/venv/pipx) and the deployment tools
# (copier, invoke, pre-commit), plus zip and logrotate.
#
# Usage: full_setup_install.sh [git_helper_image]
#   <git_helper_image>  the git helper image (doodba's by digest) that the
#                       deploys fetch their repositories with, pulled now
#                       and kept under its local tag with the label the
#                       daily prune spares, so a first deploy does not wait
#                       for it
#
# Every step is idempotent — re-running only installs what is missing, so
# this doubles as a repair. Driven by ``full_setup_executor``; the per-tool
# verification (and its pass/fail judgement) stays in the executor so the
# panel can report each tool individually.
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# pipx installs land in the invoking user's ~/.local/bin, which is not yet
# on PATH in this non-login shell; prepend it for the pipx calls.
export PATH="$HOME/.local/bin:$PATH"

APT="sudo DEBIAN_FRONTEND=noninteractive apt-get"

ic_log "Updating package index..."
$APT update -qq || ic_die "apt-get update failed"

ic_log "Installing git..."
command -v git >/dev/null 2>&1 || $APT install -y -qq git || ic_die "git install failed"

ic_log "Seeding git default branch (master)..."
git config --global --get init.defaultBranch >/dev/null 2>&1 \
    || git config --global init.defaultBranch master

ic_log "Installing Docker CE..."
command -v docker >/dev/null 2>&1 \
    || (curl -fsSL https://get.docker.com | sudo sh) \
    || ic_die "Docker install failed"

# Docker's own address pools hold about 30 networks and each instance takes one,
# so a host would stop deploying long before running out of memory or disk. Same
# values as host_hardening, which has already written them on a hardened host.
# Only with no containers: the option needs Docker restarted, which without
# live-restore stops them; hardening a host that is already running applies it.
ic_log "Widening Docker's address pools..."
daemon_json="${IC_DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
if ! sudo grep -q default-address-pools "$daemon_json" 2>/dev/null; then
    if [ -n "$(sudo docker ps -aq 2>/dev/null)" ]; then
        ic_warn "containers are running: Docker's address pools stay as they are (about 30 networks)"
    else
        merged="$( (sudo cat "$daemon_json" 2>/dev/null || echo '{}') | python3 -c '
import json, sys
config = json.load(sys.stdin)
config["bip"] = "172.17.0.1/16"
config["default-address-pools"] = [{"base": "10.200.0.0/14", "size": 24}]
print(json.dumps(config, indent=2))
')"
        if [ -n "$merged" ]; then
            sudo mkdir -p "$(dirname "$daemon_json")"
            if ! { printf '%s\n' "$merged" | sudo tee "$daemon_json" >/dev/null \
                && sudo systemctl restart docker; }; then
                ic_warn "could not widen Docker's address pools"
            fi
        fi
    fi
fi

ic_log "Adding $USER to the docker group..."
groups | grep -q docker \
    || sudo usermod -aG docker "$USER" \
    || ic_warn "could not add $USER to docker group"

ic_log "Installing the Docker Compose plugin..."
docker compose version >/dev/null 2>&1 \
    || $APT install -y -qq docker-compose-plugin \
    || ic_die "docker-compose-plugin install failed"

ic_log "Installing python3-pip and python3-venv..."
dpkg -s python3-pip python3-venv >/dev/null 2>&1 \
    || $APT install -y -qq python3-pip python3-venv \
    || ic_die "python3-pip/venv install failed"

ic_log "Installing pipx..."
command -v pipx >/dev/null 2>&1 \
    || python3 -m pip install --break-system-packages --user pipx 2>/dev/null \
    || python3 -m pip install --user pipx \
    || ic_die "pipx install failed"

ic_log "Ensuring ~/.local/bin on PATH in ~/.bashrc..."
# SC2016: the single quotes are intentional — write the literal
# ``$HOME`` to ~/.bashrc so it expands at each login, not now.
# shellcheck disable=SC2016
grep -q '.local/bin' ~/.bashrc 2>/dev/null \
    || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

# Copier pinned: it runs every deploy, and how it behaves must not depend on the
# day a host was set up (its cache came with an unasked-for upgrade).
for tool in copier==9.19.1 invoke pre-commit; do
    ic_log "Installing $tool (pipx)..."
    pipx install "$tool" 2>/dev/null || pipx upgrade "${tool%%==*}" \
        || ic_warn "pipx install/upgrade of $tool reported an issue"
done

ic_log "Installing zip..."
command -v zip >/dev/null 2>&1 || $APT install -y -qq zip \
    || ic_die "zip install failed"

# Rotates each instance's Odoo log into the dated archive the panel
# reads back (see scripts/instance_logs.sh). Installed here so a fresh
# host is ready before the first deploy instead of pulling a package in
# the middle of one.
ic_log "Installing logrotate..."
command -v logrotate >/dev/null 2>&1 || $APT install -y -qq logrotate \
    || ic_warn "logrotate install failed: instance logs will not be archived"

if [ -n "${1:-}" ]; then
    ic_log "Pulling the git helper image..."
    if sudo docker pull -q "$1" >/dev/null; then
        ic_git_helper_prepare "$1" sudo docker \
            || ic_warn "could not tag the git helper image: the first deploy that needs it does"
    else
        ic_warn "could not pull the git helper image: the first deploy that needs it pulls it"
    fi
fi

ic_log "Host toolchain installation complete."
