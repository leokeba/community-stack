#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

usage() {
    printf 'Usage: VPS_HOST=<host-or-ip> [VPS_USER=debian] [REMOTE_DIR=/opt/community-stack] %s\n' "$0"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

vps_host="${VPS_HOST:-}"
vps_user="${VPS_USER:-debian}"
remote_dir="${REMOTE_DIR:-/opt/community-stack}"

if [[ -z "$vps_host" ]]; then
    usage >&2
    printf 'VPS_HOST is required.\n' >&2
    exit 2
fi
if [[ ! "$vps_host" =~ ^[A-Za-z0-9._-]+$ || ! "$vps_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
    printf 'Invalid SSH host or user.\n' >&2
    exit 2
fi
if [[ ! "$remote_dir" =~ ^/[A-Za-z0-9._/-]+$ \
    || "$remote_dir" == *"/../"* \
    || "$remote_dir" == */.. \
    || "$remote_dir" == *"/./"* \
    || "$remote_dir" == */. ]]; then
    printf 'REMOTE_DIR must be an absolute path containing only path characters.\n' >&2
    exit 2
fi

for command in git rsync ssh tar uv docker; do
    command -v "$command" >/dev/null || {
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    }
done

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    printf 'Refusing to deploy a dirty worktree. Commit or discard changes first.\n' >&2
    exit 1
fi

commit="$(git rev-parse --verify HEAD)"
deploy_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
target="${vps_user}@${vps_host}"
# Fail within about a minute if the connection dies (for example when the laptop
# sleeps) instead of hanging forever.
ssh_opts=(-o ServerAliveInterval=15 -o ServerAliveCountMax=4)

printf 'Running local tests and deployment configuration checks...\n'
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
docker compose -f services/announcements/docker-compose.yml config --quiet
docker compose -f services/proxy/docker-compose.yml config --quiet
docker compose -f services/landing/docker-compose.yml config --quiet

printf 'Checking SSH, backup, and Docker prerequisites on %s...\n' "$target"
ssh "${ssh_opts[@]}" -o BatchMode=yes -o ConnectTimeout=10 "$target" bash -s -- "$remote_dir" <<'REMOTE'
set -eu
project_dir=$1
test -d "$project_dir"
sudo -n -u community test -f "$project_dir/community-stack.yaml"
sudo -n -u community test -f "$project_dir/.env"
sudo -n -u community test -x "$project_dir/.venv/bin/community-stack"
sudo -n true
sudo -n docker info >/dev/null
sudo -n -u community test -w "$project_dir"
sudo -n -u community test -w "$project_dir/.venv"
REMOTE

printf 'Creating and verifying a restrictive pre-deployment backup...\n'
ssh "${ssh_opts[@]}" "$target" bash -s -- "$remote_dir" <<'REMOTE'
set -eu
project_dir=$1
sudo -n env PYTHONDONTWRITEBYTECODE=1 sh -c "umask 077; cd '$project_dir' && .venv/bin/community-stack backup create"
sudo -n env PYTHONDONTWRITEBYTECODE=1 sh -c "cd '$project_dir' && .venv/bin/community-stack backup verify"
REMOTE

printf 'Checking the remote rsync prerequisite...\n'
ssh "${ssh_opts[@]}" "$target" bash -s <<'REMOTE'
set -eu
if ! command -v rsync >/dev/null 2>&1; then
    sudo -n apt-get update -q
    sudo -n apt-get install -y -q rsync
fi
REMOTE

stage="$(mktemp -d "${TMPDIR:-/tmp}/community-stack-deploy.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
git archive --format=tar "$commit" | tar -xf - -C "$stage"
# rsync -a copies this folder's mode onto the remote project directory. Keep it
# 0700 on purpose: see "Accounts and file ownership" in docs/deployment.md.
chmod 0700 "$stage"

printf 'Transferring committed source while preserving production config and data...\n'
rsync -a -e "ssh ${ssh_opts[*]}" \
    --rsync-path='sudo -n -u community rsync' \
    --exclude='/.env' \
    --exclude='/community-stack.yaml' \
    --exclude='/data/***' \
    --exclude='/backups/***' \
    --exclude='/services/proxy/Caddyfile' \
    "$stage/" "$target:$remote_dir/"

printf 'Building and checking the announcements service on the VPS...\n'
ssh "${ssh_opts[@]}" "$target" bash -s -- "$remote_dir" "$deploy_id" <<'REMOTE'
set -eu
project_dir=$1
deploy_id=$2

# The host CLI and the control agent run from this editable install; keep its
# dependencies in step with the code that was just copied.
uv_bin="$(sudo -n -u community sh -c 'command -v uv || echo "$HOME/.local/bin/uv"')"
if sudo -n -u community test -x "$uv_bin"; then
    sudo -n -u community bash -c 'cd "$1" && "$2" sync --frozen --no-dev --quiet' \
        _ "$project_dir" "$uv_bin"
else
    # Servers set up with scripts/bootstrap.sh use pip.
    sudo -n -u community "$project_dir/.venv/bin/pip" install --quiet -e "$project_dir"
fi
sudo -n -u community "$project_dir/.venv/bin/community-stack" --help >/dev/null
sudo -n -u community "$project_dir/.venv/bin/python" - "$project_dir" <<'PY'
from pathlib import Path
import sys

from community_stack.config import load_config
from community_stack.provision import prepare_facilmap, prepare_vikunja

root = Path(sys.argv[1])
config = load_config(root / "community-stack.yaml")
prepare_vikunja(root, config, root / ".env")
prepare_facilmap(root, config, root / ".env")
PY
# Keep the bridge from syncing while its persistent config is migrated. The
# helper preserves the existing login, secrets, and explicitly selected groups.
if bridge_state="$(sudo -n docker inspect community_whatsapp_bridge \
    --format '{{.State.Status}}' 2>/dev/null)"; then
    if [[ "$bridge_state" == running ]]; then
        sudo -n docker stop community_whatsapp_bridge >/dev/null
    fi
else
    container_names="$(sudo -n docker container ls -a --format '{{.Names}}')"
    if printf '%s\n' "$container_names" | grep -Fxq community_whatsapp_bridge; then
        printf 'Could not inspect the existing WhatsApp bridge container.\n' >&2
        exit 1
    fi
fi
sudo -n "$project_dir/.venv/bin/python" - "$project_dir" <<'PY'
from pathlib import Path
import sys

from community_stack.provision import ensure_whatsapp_privacy_config

config = Path(sys.argv[1]) / "data/whatsapp_bridge/config.yaml"
if config.is_file():
    ensure_whatsapp_privacy_config(config)
PY
if [[ -n "${bridge_state:-}" ]]; then
    bridge_state="$(sudo -n docker inspect community_whatsapp_bridge \
        --format '{{.State.Status}}')"
    if [[ "$bridge_state" == running ]]; then
        printf 'WhatsApp bridge is still running after its privacy migration.\n' >&2
        exit 1
    fi
fi
# The announcements container runs as the UID/GID in .env (COMMUNITY_STACK_UID/GID), which
# is not always 1000. Read only those two numbers, and give the contact page folder that
# owner before Docker can create it as root.
container_uid="$(sudo -n sh -c "grep -E '^COMMUNITY_STACK_UID=' '$project_dir/.env' | cut -d= -f2" || true)"
container_gid="$(sudo -n sh -c "grep -E '^COMMUNITY_STACK_GID=' '$project_dir/.env' | cut -d= -f2" || true)"
container_uid="${container_uid:-1000}"
container_gid="${container_gid:-1000}"
if [[ ! "$container_uid" =~ ^[0-9]+$ || ! "$container_gid" =~ ^[0-9]+$ ]]; then
    printf 'COMMUNITY_STACK_UID and COMMUNITY_STACK_GID must be numbers.\n' >&2
    exit 1
fi
sudo -n install -d -m 0755 -o "$container_uid" -g "$container_gid" \
    "$project_dir/data/landing/connect" "$project_dir/data/landing/continuity"

docker_cmd=(sudo -n docker)
compose_shell='cd "$1" && shift && exec "$@"'
compose_app=(sudo -n sh -c "$compose_shell" sh "$project_dir" docker compose --env-file .env -f services/announcements/docker-compose.yml)
compose_proxy=(sudo -n sh -c "$compose_shell" sh "$project_dir" docker compose --env-file .env -f services/proxy/docker-compose.yml)
compose_landing=(sudo -n sh -c "$compose_shell" sh "$project_dir" docker compose -f services/landing/docker-compose.yml)
rollback_tag="community-stack-announcements:rollback-$deploy_id"
old_image="$("${docker_cmd[@]}" image inspect community-stack-announcements:0.1.0 --format '{{.Id}}' 2>/dev/null || true)"
if [ -n "$old_image" ]; then
    "${docker_cmd[@]}" image tag "$old_image" "$rollback_tag"
else
    rollback_tag="unavailable"
fi

"${compose_app[@]}" config --quiet
"${compose_app[@]}" build announcements
"${compose_app[@]}" up -d --force-recreate --no-build announcements

healthy=false
for attempt in $(seq 1 36); do
    status="$("${docker_cmd[@]}" inspect community_announcements --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)"
    if [ "$status" = healthy ]; then
        healthy=true
        break
    fi
    state="$("${docker_cmd[@]}" inspect community_announcements --format '{{.State.Status}}' 2>/dev/null || echo missing)"
    if [ "$state" = exited ] || [ "$state" = dead ]; then
        break
    fi
    sleep 5
done

if [ "$healthy" != true ]; then
    "${docker_cmd[@]}" logs --tail 80 community_announcements || true
    if [ -n "$old_image" ]; then
        "${docker_cmd[@]}" image tag "$rollback_tag" community-stack-announcements:0.1.0
        "${compose_app[@]}" up -d --force-recreate --no-build announcements
    fi
    echo "Announcements health check failed; the prior image was restored when available." >&2
    exit 1
fi

# Render the contact page now, from inside the container that owns the data. If this
# fails the page is rebuilt after the next delivery, so it is not fatal.
"${docker_cmd[@]}" exec -u "$container_uid:$container_gid" community_announcements \
    community-stack connect render --output-dir /app/data/landing/connect \
    || printf 'Warning: the contact page was not rendered; it updates after the next delivery.\n' >&2

sudo -n -u community python3 - "$project_dir" "$deploy_id" <<'PY'
from pathlib import Path
import shutil
import sys

project_dir = Path(sys.argv[1])
path = project_dir / "services/proxy/Caddyfile"
text = path.read_text(encoding="utf-8")
routes = [
    "    handle /admin* {\n        reverse_proxy community_announcements:8080\n    }\n\n",
    "    handle /ap/* {\n        reverse_proxy community_announcements:8080\n    }\n"
    "    handle /.well-known/webfinger {\n        reverse_proxy community_announcements:8080\n    }\n\n",
    "    handle /subscribe* {\n        reverse_proxy community_announcements:8080\n    }\n\n",
]
backup = project_dir / "backups" / f"Caddyfile.before-{sys.argv[2]}"
backup.parent.mkdir(parents=True, exist_ok=True)
if backup.exists():
    raise SystemExit(f"Refusing to overwrite rollback file {backup}.")
shutil.copy2(path, backup)
backup.chmod(0o600)
missing = [route for route in routes if route not in text]
if missing:
    anchor = "    handle /feed* {\n        reverse_proxy community_landing:80\n    }\n"
    if text.count(anchor) != 1:
        raise SystemExit("Cannot safely locate the live public-feed route in Caddyfile.")
    path.write_text(text.replace(anchor, "".join(missing) + anchor), encoding="utf-8")
    path.chmod(0o644)
text = path.read_text(encoding="utf-8")
route_import = "import /etc/caddy/vikunja.caddy"
route_imports = [route_import, "import /etc/caddy/facilmap.caddy"]
missing_imports = [entry for entry in route_imports if entry not in text]
if missing_imports:
    path.write_text(text.rstrip() + "\n\n" + "\n".join(missing_imports) + "\n", encoding="utf-8")
    path.chmod(0o644)
PY

caddy_backup="$project_dir/backups/Caddyfile.before-$deploy_id"
if ! "${compose_proxy[@]}" run --rm --no-deps --entrypoint caddy caddy \
    validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
    sudo -n -u community install -m 0644 "$caddy_backup" \
        "$project_dir/services/proxy/Caddyfile"
    exit 1
fi
printf 'Recreating Caddy with the generated optional-service routes...\n'
if ! sudo -n docker compose --env-file "$project_dir/.env" \
    -f "$project_dir/services/proxy/docker-compose.yml" \
    up -d --force-recreate --no-deps caddy; then
    sudo -n -u community install -m 0644 "$caddy_backup" \
        "$project_dir/services/proxy/Caddyfile"
    sudo -n docker compose --env-file "$project_dir/.env" \
        -f "$project_dir/services/proxy/docker-compose.yml" \
        up -d --force-recreate --no-deps caddy || true
    exit 1
fi

proxy_mounts="$("${docker_cmd[@]}" inspect community_proxy \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}')"
for route in /etc/caddy/vikunja.caddy /etc/caddy/facilmap.caddy; do
    if ! printf '%s\n' "$proxy_mounts" | grep -qx "$route"; then
        "${docker_cmd[@]}" logs --tail 80 community_proxy || true
        printf 'Caddy is missing the generated route mount: %s\n' "$route" >&2
        exit 1
    fi
done

proxy_running=false
for attempt in $(seq 1 12); do
    state_line="$("${docker_cmd[@]}" inspect community_proxy \
        --format '{{.State.Status}} {{.RestartCount}}' 2>/dev/null || echo missing)"
    read -r proxy_state proxy_restarts <<<"$state_line"
    if [[ "$proxy_state" == running ]]; then
        sleep 5
        next_state_line="$("${docker_cmd[@]}" inspect community_proxy \
            --format '{{.State.Status}} {{.RestartCount}}' 2>/dev/null || echo missing)"
        read -r next_proxy_state next_proxy_restarts <<<"$next_state_line"
        if [[ "$next_proxy_state" == running && "$next_proxy_restarts" == "$proxy_restarts" ]]; then
            proxy_running=true
            break
        fi
    elif [[ "$proxy_state" == exited || "$proxy_state" == dead ]]; then
        break
    fi
    sleep 2
done
if [[ "$proxy_running" != true ]]; then
    "${docker_cmd[@]}" logs --tail 80 community_proxy || true
    printf 'Caddy did not remain running after recreation.\n' >&2
    exit 1
fi

printf 'Deployment healthy. Prior image tag: %s\n' "${rollback_tag:-unavailable}"
REMOTE

printf 'Verifying the proxy routes and restarting the control agent...\n'
ssh "${ssh_opts[@]}" "$target" bash -s -- "$remote_dir" <<'REMOTE'
set -eu
project_dir=$1
docker_cmd=(sudo -n docker)
compose_landing=(sudo -n docker compose -f "$project_dir/services/landing/docker-compose.yml")

proxy_mounts="$("${docker_cmd[@]}" inspect community_proxy \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}')"
missing_route=false
for route in /etc/caddy/vikunja.caddy /etc/caddy/facilmap.caddy; do
    if ! printf '%s\n' "$proxy_mounts" | grep -qx "$route"; then
        missing_route=true
    fi
done
if [[ "$missing_route" == true ]]; then
    sudo -n docker compose --env-file "$project_dir/.env" \
        -f "$project_dir/services/proxy/docker-compose.yml" \
        up -d --force-recreate --no-deps caddy
    proxy_mounts="$("${docker_cmd[@]}" inspect community_proxy \
        --format '{{range .Mounts}}{{println .Destination}}{{end}}')"
    for route in /etc/caddy/vikunja.caddy /etc/caddy/facilmap.caddy; do
        if ! printf '%s\n' "$proxy_mounts" | grep -qx "$route"; then
            "${docker_cmd[@]}" logs --tail 80 community_proxy || true
            printf 'Caddy is missing the generated route mount: %s\n' "$route" >&2
            exit 1
        fi
    done
fi

proxy_running=false
for attempt in $(seq 1 12); do
    state_line="$("${docker_cmd[@]}" inspect community_proxy \
        --format '{{.State.Status}} {{.RestartCount}}' 2>/dev/null || echo missing)"
    read -r proxy_state proxy_restarts <<<"$state_line"
    if [[ "$proxy_state" == running ]]; then
        sleep 5
        next_state_line="$("${docker_cmd[@]}" inspect community_proxy \
            --format '{{.State.Status}} {{.RestartCount}}' 2>/dev/null || echo missing)"
        read -r next_proxy_state next_proxy_restarts <<<"$next_state_line"
        if [[ "$next_proxy_state" == running && "$next_proxy_restarts" == "$proxy_restarts" ]]; then
            proxy_running=true
            break
        fi
    elif [[ "$proxy_state" == exited || "$proxy_state" == dead ]]; then
        break
    fi
    sleep 2
done
if [[ "$proxy_running" != true ]]; then
    "${docker_cmd[@]}" logs --tail 80 community_proxy || true
    printf 'Caddy did not remain running after deployment.\n' >&2
    exit 1
fi

"${compose_landing[@]}" up -d --force-recreate landing
"${docker_cmd[@]}" inspect community_announcements --format '{{.State.Health.Status}}' | grep -qx healthy
sudo -n bash "$project_dir/scripts/restart-agent.sh"
printf 'Deployment services are active and verified.\n'
REMOTE

printf 'Deployed commit %s to %s:%s\n' "$commit" "$target" "$remote_dir"
