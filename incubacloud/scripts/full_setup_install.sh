#!/usr/bin/env bash
# Install the host toolchain for a full setup: git, Docker CE, Compose v2,
# the Python toolchain (pip/venv/pipx) and the deployment tools
# (copier, invoke, pre-commit), plus zip and logrotate.
#
# Usage: full_setup_install.sh
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

for tool in copier invoke pre-commit; do
    ic_log "Installing $tool (pipx)..."
    pipx install "$tool" 2>/dev/null || pipx upgrade "$tool" \
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

ic_log "Host toolchain installation complete."
