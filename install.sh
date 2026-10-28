#!/usr/bin/env bash
# Interactive single-command installer for Outline WSS Docker node.
set -Eeuo pipefail
umask 077

REPO_SLUG="PavelShestakov/outline-wss"  # Set after publishing the repository.
BRANCH="main"
TARGET_DIR="/opt/outline-wss-docker"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
msg() { printf '\n==> %s\n' "$*"; }
ask() {
  local var="$1" label="$2" default="${3:-}" answer
  if [[ -n "$default" ]]; then
    read -r -p "$label [$default]: " answer </dev/tty || err 'Interactive terminal required'
    printf -v "$var" '%s' "${answer:-$default}"
  else
    read -r -p "$label: " answer </dev/tty || err 'Interactive terminal required'
    printf -v "$var" '%s' "$answer"
  fi
}
port_valid() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)); }
host_valid() { [[ "$1" =~ ^[a-zA-Z0-9.-]+$ ]] && [[ "$1" == *.* ]] && [[ "$1" != *..* ]]; }
allow_valid() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/([0-9]|[12][0-9]|3[012]))?$ ]]; }

(( EUID == 0 )) || err 'Run as root: sudo bash install.sh'
[[ -r /etc/os-release ]] || err 'Unsupported OS'
# shellcheck disable=SC1091
. /etc/os-release
[[ "$ID" == ubuntu || "$ID" == debian ]] || err 'Ubuntu and Debian are supported'
[[ "$(uname -m)" == x86_64 || "$(uname -m)" == aarch64 ]] || err 'Only amd64 / arm64 are supported'
missing=()
for package_cmd in openssl curl tar python3 ss; do
  command -v "$package_cmd" >/dev/null 2>&1 || missing+=("$package_cmd")
done
if ((${#missing[@]})); then
  apt-get update -qq
  apt-get install -y ca-certificates curl tar openssl python3 iproute2
fi

cat <<'TXT'
Outline WSS Docker installer
 - Cloudflare Tunnel must already exist (remotely managed).
 - Create public hostname routes in the Cloudflare dashboard after installation.
 - New/empty VPS recommended. This script does NOT import an old node.
TXT

ask WSS_HOST 'WSS hostname (example: srv.example.org)'
ask KEYS_HOST 'Dynamic key hostname (example: conn.example.org)'
ask WSS_PORT 'Local WebSocket port' '636'
ask API_PORT 'Public HTTPS management API port' '18443'
ask API_HOST 'Public IPv4 address or DNS name for API certificate'
ask ALLOWED_IP 'Allowed IPv4 of the controller (/32), e.g. 203.0.113.10/32'
ask CONFIRM_DIR 'Install directory' "$TARGET_DIR"
TARGET_DIR="$CONFIRM_DIR"

host_valid "$WSS_HOST" || err 'Invalid WSS hostname'
host_valid "$KEYS_HOST" || err 'Invalid config hostname'
port_valid "$WSS_PORT" || err 'Invalid local WebSocket port'
port_valid "$API_PORT" || err 'Invalid API port'
[[ "$WSS_PORT" != 18081 && "$WSS_PORT" != 18443 ]] || err 'WSS port conflicts with internal services'
[[ "$API_PORT" != "$WSS_PORT" ]] || err 'API and WebSocket ports must differ'
allow_valid "$ALLOWED_IP" || err 'Only IPv4 / IPv4 CIDR allowed for controller'
# Detect if API hostname is an IPv4 address or domain. Validate IPv4 octets.
if [[ "$API_HOST" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  API_SAN="IP:${API_HOST}"
else
  host_valid "$API_HOST" || err 'Invalid API hostname'
  API_SAN="DNS:${API_HOST}"
fi
python3 - "$ALLOWED_IP" "$API_HOST" <<'PY' || err 'Invalid IP or network'
import ipaddress,sys
ipaddress.IPv4Network(sys.argv[1], strict=False)
try:
    ipaddress.IPv4Address(sys.argv[2])
except ValueError:
    if all(x.isdigit() or x == '.' for x in sys.argv[2]):
        raise ValueError('Invalid IPv4 address')
PY
[[ "$TARGET_DIR" = /* && "$TARGET_DIR" != '/' ]] || err 'Install directory must be absolute and non-root'
[[ ! -e "$TARGET_DIR/.env" ]] || err "Already configured at $TARGET_DIR; refusing to overwrite credentials"

printf 'Cloudflare Tunnel token (input hidden): ' >/dev/tty
IFS= read -rs CLOUDFLARE_TUNNEL_TOKEN </dev/tty || err 'TTY required'
printf '\n' >/dev/tty
[[ -n "$CLOUDFLARE_TUNNEL_TOKEN" && "$CLOUDFLARE_TUNNEL_TOKEN" != *$'\n'* && "$CLOUDFLARE_TUNNEL_TOKEN" != *' '* ]] || err 'Invalid tunnel token'

msg 'Installing Docker Engine + Docker Compose if needed'
install_docker_repo() {
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  local suite arch
  suite="${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}"
  arch="$(dpkg --print-architecture)"
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/${ID}
Suites: ${suite}
Components: stable
Architectures: ${arch}
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -y
}
if ! command -v docker >/dev/null 2>&1; then
  install_docker_repo
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
elif ! docker compose version >/dev/null 2>&1; then
  msg 'Docker is present but Compose plugin is missing'
  install_docker_repo
  if ! apt-get install -y docker-compose-plugin; then
    err 'Install docker-compose-plugin manually. Existing Docker packages were not removed.'
  fi
fi
systemctl enable --now docker

if ss -H -tln "sport = :${API_PORT}" | grep -q .; then
  err "API port ${API_PORT}/tcp is already in use. Stop the existing service or choose another port."
fi

msg 'Acquiring project sources'
# Local checkout: bash /path/to/outline-wss/install.sh
SOURCE_DIR=''
if [[ "${BASH_SOURCE[0]:-}" != 'bash' && "${BASH_SOURCE[0]:-}" != '-c' && -f "$(dirname -- "${BASH_SOURCE[0]}")/compose.yaml" ]]; then
  SOURCE_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi
mkdir -p "$TARGET_DIR" "$TARGET_DIR/config/tls" "$TARGET_DIR/data/keys" "$TARGET_DIR/data/manager"
chmod 700 "$TARGET_DIR/config" "$TARGET_DIR/config/tls" "$TARGET_DIR/data" "$TARGET_DIR/data/keys" "$TARGET_DIR/data/manager"
if [[ -n "$SOURCE_DIR" ]]; then
  [[ "$SOURCE_DIR" != "$TARGET_DIR" ]] || err 'Run installer from a separate checkout, not target directory'
  cp -a "$SOURCE_DIR/app" "$SOURCE_DIR/Dockerfile" "$SOURCE_DIR/requirements.txt" "$SOURCE_DIR/compose.yaml" "$TARGET_DIR/"
else
  [[ "$REPO_SLUG" != PavelShestakov/* ]] || err 'Repository owner has not been set in published install.sh'
  archive="$(mktemp)"
  trap 'rm -f "${archive:-}"' EXIT
  curl -fsSL --retry 3 "https://github.com/${REPO_SLUG}/archive/refs/heads/${BRANCH}.tar.gz" -o "$archive"
  tar -xzf "$archive" --strip-components=1 -C "$TARGET_DIR" \
    --wildcards --no-anchored 'app/*' 'Dockerfile' 'requirements.txt' 'compose.yaml' || err 'Could not unpack repository'
  rm -f "$archive"
  trap - EXIT
fi

msg 'Creating private settings / TLS certificate'
ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
WS_PATH="$(python3 -c 'import secrets; print(secrets.token_urlsafe(30))')"
cat > "$TARGET_DIR/data/config.yaml" <<EOF
web:
  servers:
    - id: outline-wss
      listen:
        - "0.0.0.0:${WSS_PORT}"
services:
  - listeners:
      - type: websocket-stream
        web_server: outline-wss
        path: "/${WS_PATH}/tcp"
      - type: websocket-packet
        web_server: outline-wss
        path: "/${WS_PATH}/udp"
    keys: []
EOF
cp "$TARGET_DIR/data/config.yaml" "$TARGET_DIR/data/manager/base.yaml"
cat > "$TARGET_DIR/config/node.env" <<EOF
OUTLINE_ADMIN_TOKEN=${ADMIN_TOKEN}
OUTLINE_WS_BASE=wss://${WSS_HOST}
OUTLINE_CONFIG_PUBLIC_BASE=https://${KEYS_HOST}
OUTLINE_WS_PATH=${WS_PATH}
OUTLINE_CONFIG=/data/config.yaml
OUTLINE_BASE_CONFIG=/data/manager/base.yaml
OUTLINE_MANAGER_DB=/data/manager/state.db
OUTLINE_KEYS_DIR=/data/keys
OUTLINE_KEYS_PORT=18081
OUTLINE_KEYS_BIND=0.0.0.0
OUTLINE_WS_PORT=${WSS_PORT}
OUTLINE_METRICS_URL=http://127.0.0.1:9091/metrics
OUTLINE_POLL_SECONDS=5
OUTLINE_METRICS_MAX_AGE=20
OUTLINE_RELOAD_MODE=pidfile
OUTLINE_PID_FILE=/run/outline-wss/outline.pid
EOF
cat > "$TARGET_DIR/.env" <<EOF
API_PORT=${API_PORT}
CLOUDFLARE_TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
EOF
openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
  -keyout "$TARGET_DIR/config/tls/server.key" \
  -out "$TARGET_DIR/config/tls/server.crt" \
  -subj '/CN=outline-wss-api' -addext "subjectAltName=${API_SAN}" >/dev/null 2>&1
cat > "$TARGET_DIR/config/nginx.conf" <<EOF
server {
    listen 443 ssl;
    server_name _;
    ssl_certificate /etc/nginx/tls/server.crt;
    ssl_certificate_key /etc/nginx/tls/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_tickets off;
    server_tokens off;
    client_max_body_size 32k;
    access_log off;

    location / {
        allow ${ALLOWED_IP};
        deny all;
        proxy_pass http://node:18443;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 5s;
        proxy_read_timeout 20s;
        add_header Cache-Control "no-store" always;
    }
}
EOF
chmod 600 "$TARGET_DIR/.env" "$TARGET_DIR/config/node.env" "$TARGET_DIR/config/tls/server.key" "$TARGET_DIR/config/tls/server.crt" "$TARGET_DIR/config/nginx.conf" "$TARGET_DIR/data/config.yaml" "$TARGET_DIR/data/manager/base.yaml"
# Node process is unprivileged inside Docker, but owns its persisted files.
chown -R 10001:10001 "$TARGET_DIR/data"
FINGERPRINT="$(openssl x509 -in "$TARGET_DIR/config/tls/server.crt" -outform der | openssl dgst -sha256 -binary | base64 -w0 | tr '+/' '-_' | tr -d '=')"
cat > "$TARGET_DIR/config/admin-url.txt" <<EOF
https://${API_HOST}:${API_PORT}/m/${ADMIN_TOKEN}#fp=${FINGERPRINT}
EOF
chmod 600 "$TARGET_DIR/config/admin-url.txt"

msg 'Building and starting Docker services'
(cd "$TARGET_DIR" && docker compose build --pull node && docker compose up -d)

cat <<EOF

Installation started. Check: cd ${TARGET_DIR} && docker compose ps
Cloudflare dashboard -> tunnel public hostname routes:
  ${WSS_HOST}  -> HTTP  http://node:${WSS_PORT}
  ${KEYS_HOST} -> HTTP  http://node:18081

API public HTTPS port: ${API_PORT}/tcp (ALLOW only ${ALLOWED_IP})
API URL + SHA256 pin (secret, do NOT share): ${TARGET_DIR}/config/admin-url.txt
View privately: sudo cat ${TARGET_DIR}/config/admin-url.txt

IMPORTANT: The API certificate is self-signed. Use included pinned client
or explicitly trust config/tls/server.crt on the controller host.
This is a clean install. It DOES NOT migrate existing Outline keys.
EOF
