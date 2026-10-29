#!/usr/bin/env bash
# Set up the Community Stack on a fresh Debian or Ubuntu server, ending at the
# browser setup page. Run as root:
#
#   sudo bash bootstrap.sh --web-domain ourtown.org --matrix-domain matrix.ourtown.org \
#        --email admin@ourtown.org --timezone Europe/Paris
#
# Safe to run again: existing configuration, secrets and data are kept.
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/leokeba/community-stack.git}"
REF="${REF:-main}"
INSTALL_DIR="/opt/community-stack"
SERVICE_USER="community"
web_domain=""
matrix_domain=""
email=""
timezone="UTC"
project_name=""
dry_run=false
skip_firewall=false

usage() {
    cat <<'USAGE'
Usage: sudo bash bootstrap.sh --web-domain DOMAIN --matrix-domain DOMAIN --email EMAIL [options]

  --web-domain DOMAIN     Website and admin console, e.g. ourtown.org
  --matrix-domain DOMAIN  Matrix chat server, e.g. matrix.ourtown.org
  --email EMAIL           Address for HTTPS certificate notices
  --timezone ZONE         IANA time zone for scheduling (default UTC)
  --name NAME             Short identifier for this community (default from the domain)
  --repo URL --ref REF    Where to get the code (default REPO_URL / REF variables)
  --dir PATH              Install location (default /opt/community-stack)
  --skip-firewall         Leave the firewall alone
  --dry-run               Print every step without changing anything
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --web-domain) web_domain="${2:-}"; shift 2 ;;
        --matrix-domain) matrix_domain="${2:-}"; shift 2 ;;
        --email) email="${2:-}"; shift 2 ;;
        --timezone) timezone="${2:-}"; shift 2 ;;
        --name) project_name="${2:-}"; shift 2 ;;
        --repo) REPO_URL="${2:-}"; shift 2 ;;
        --ref) REF="${2:-}"; shift 2 ;;
        --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
        --skip-firewall) skip_firewall=true; shift ;;
        --dry-run) dry_run=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }
run() {
    if $dry_run; then
        printf '    [dry run] %s\n' "$*"
    else
        "$@"
    fi
}
as_service_user() { run runuser -u "$SERVICE_USER" -- "$@"; }

domain_re='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'
[[ -n "$web_domain" && -n "$matrix_domain" && -n "$email" ]] || { usage >&2; exit 2; }
[[ "$web_domain" =~ $domain_re && "$matrix_domain" =~ $domain_re ]] \
    || fail "Domains must be lowercase names such as ourtown.org."
[[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || fail "That email address does not look right."
[[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "--dir must be an absolute path."
if [[ -z "$project_name" ]]; then
    project_name="$(printf '%s' "${web_domain%%.*}" | tr -c 'a-z0-9_-' '-' | cut -c1-40)"
fi

say "Checking this server"
$dry_run || [[ $EUID -eq 0 ]] || fail "Run this as root (for example with sudo)."
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    debian:12|debian:13|ubuntu:22.04|ubuntu:24.04) printf '    %s %s\n' "$ID" "$VERSION_ID" ;;
    *) fail "Supported systems: Debian 12/13, Ubuntu 22.04/24.04 (found ${PRETTY_NAME:-unknown})." ;;
esac
for domain in "$web_domain" "$matrix_domain"; do
    if getent ahosts "$domain" >/dev/null; then
        printf '    %s resolves to %s\n' "$domain" "$(getent ahosts "$domain" | awk 'NR==1{print $1}')"
    else
        printf '    [WARN] %s does not resolve yet; HTTPS certificates will wait until it does.\n' "$domain"
    fi
done

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -q
run apt-get install -y -q ca-certificates curl gnupg git python3 python3-venv restic ufw

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    say "Installing Docker from Docker's own repository"
    run install -m 0755 -d /etc/apt/keyrings
    run curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    run chmod a+r /etc/apt/keyrings/docker.asc
    arch="$(dpkg --print-architecture)"
    line="deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable"
    if $dry_run; then
        printf '    [dry run] write /etc/apt/sources.list.d/docker.list: %s\n' "$line"
    else
        printf '%s\n' "$line" >/etc/apt/sources.list.d/docker.list
    fi
    run apt-get update -q
    run apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
run systemctl enable --now docker

say "Creating the '$SERVICE_USER' account that owns the stack"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    run useradd --system --create-home --shell /bin/bash "$SERVICE_USER"
fi
uid="$(id -u "$SERVICE_USER" 2>/dev/null || echo 1000)"
gid="$(id -g "$SERVICE_USER" 2>/dev/null || echo 1000)"

say "Getting the code into $INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
    as_service_user git -C "$INSTALL_DIR" fetch --depth 1 origin "$REF"
    as_service_user git -C "$INSTALL_DIR" checkout --quiet FETCH_HEAD
elif [[ -e "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    fail "$INSTALL_DIR exists and is not a git checkout; move it away or choose --dir."
else
    run install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR"
    as_service_user git clone --quiet --depth 1 --branch "$REF" "$REPO_URL" "$INSTALL_DIR"
fi

say "Installing the community-stack tool"
as_service_user python3 -m venv "$INSTALL_DIR/.venv"
as_service_user "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
as_service_user "$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"

say "Writing the configuration and generating secrets"
setup_token_flag=()
if ! grep -q '^COMMUNITY_STACK_ADMIN_SETUP_TOKEN=' "$INSTALL_DIR/.env" 2>/dev/null; then
    setup_token_flag=(--setup-token)
fi
as_service_user "$INSTALL_DIR/.venv/bin/community-stack" init --dir "$INSTALL_DIR" \
    --project-name "$project_name" --base-domain "$web_domain" \
    --web-domain "$web_domain" --matrix-domain "$matrix_domain" \
    --timezone "$timezone" --acme-email "$email" \
    --console-uid "$uid" --console-gid "$gid" "${setup_token_flag[@]}"

if ! $skip_firewall; then
    say "Opening only SSH, HTTP and HTTPS in the firewall"
    # Keep every port sshd listens on open, so this cannot lock you out.
    for port in $(ss -Hltnp 2>/dev/null | awk '/sshd/ {sub(/.*:/, "", $4); print $4}' | sort -u) 22; do
        run ufw allow "${port}/tcp"
    done
    run ufw allow 80/tcp
    run ufw allow 443/tcp
    run ufw --force enable
fi

say "Starting the services (the first start downloads images and can take a few minutes)"
run env PYTHONDONTWRITEBYTECODE=1 sh -c "cd '$INSTALL_DIR' && .venv/bin/community-stack install"

say "Starting the control agent"
unit=/etc/systemd/system/community-stack-agent.service
if $dry_run; then
    printf '    [dry run] install %s with %s and uid/gid %s/%s\n' "$unit" "$INSTALL_DIR" "$uid" "$gid"
else
    sed -e "s#/opt/community-stack#${INSTALL_DIR}#g" \
        -e "s#--console-uid 1000#--console-uid ${uid}#" \
        -e "s#--console-gid 1000#--console-gid ${gid}#" \
        "$INSTALL_DIR/services/agent/community-stack-agent.service" >"$unit"
    chmod 0644 "$unit"
fi
run systemctl daemon-reload
run systemctl enable --now community-stack-agent

token="$(sed -n 's/^COMMUNITY_STACK_ADMIN_SETUP_TOKEN=//p' "$INSTALL_DIR/.env" 2>/dev/null || true)"
cat <<DONE

The Community Stack is installed.

  1. Open https://${web_domain}/admin/setup
     (HTTPS can take a minute after DNS points here.)
  2. Enter this setup token: ${token:-"(see COMMUNITY_STACK_ADMIN_SETUP_TOKEN in ${INSTALL_DIR}/.env)"}
  3. Create the first owner account, then follow the "Get started" checklist.

Keep a copy of RESTIC_PASSWORD from ${INSTALL_DIR}/.env somewhere safe and
offline: encrypted backups cannot be restored without it.
DONE
