#!/bin/sh
# Full-stack smoke harness. It creates disposable TLS/auth material and uses
# only the immutable images supplied by the release runner.
set -eu

compose_file=${CADASTRE_COMPOSE_FILE:-compose.production.yaml}
export CADASTRE_IMAGE=${CADASTRE_IMAGE:-cadastre:test}
export CADASTRE_GUI_IMAGE=${CADASTRE_GUI_IMAGE:-cadastre-gui:test}
test -f "$compose_file"

project=${CADASTRE_STACK_PROJECT:-cadastre-smoke-$$}
root=$(mktemp -d)
tls_dir="$root/tls"
auth_dir="$root/auth"
mkdir -p "$tls_dir" "$auth_dir"
chmod 755 "$root" "$tls_dir" "$auth_dir"
token_file="$auth_dir/tokens"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "$tls_dir/tls.key" -out "$tls_dir/tls.crt" \
  -subj '/CN=localhost' \
  -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' >/dev/null 2>&1
token=$(openssl rand -hex 24)
cat > "$token_file" <<EOF
{"tokens":[{"token":"$token","principal":"stack-smoke","audience":"cadastre","scopes":["mcp","catalog.read","catalog.check","catalog.write"]}]}
EOF
chmod 600 "$token_file"
chmod 644 "$tls_dir/tls.crt" "$tls_dir/tls.key" "$token_file"
override="$root/smoke-compose.yaml"
cat > "$override" <<'YAML'
services:
  cadastre-api:
    command: [serve, --bind, 0.0.0.0:8000, --profile, direct-https, --allow-non-loopback, --require-auth, --allow-write, --tls-cert, /run/cadastre/tls/tls.crt, --tls-key, /run/cadastre/tls/tls.key, --token-file, /run/cadastre/auth/tokens]
YAML
api_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
mcp_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
gui_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
export CADASTRE_TLS_DIR="$tls_dir"
export CADASTRE_AUTH_DIR="$auth_dir"
export CADASTRE_API_PORT="$api_port"
export CADASTRE_MCP_PORT="$mcp_port"
export CADASTRE_GUI_PORT="$gui_port"
export CADASTRE_API_ORIGIN="https://127.0.0.1:$api_port"
api_url="$CADASTRE_API_ORIGIN"
mcp_url="https://127.0.0.1:$mcp_port/mcp"
gui_url="http://127.0.0.1:$gui_port"
token_config=$(mktemp)
brief=$(mktemp)
lookup=$(mktemp)
mcp_response=$(mktemp)
cleanup() {
  rm -f "$token_config" "$brief" "$lookup" "$mcp_response"
  docker compose -p "$project" -f "$compose_file" \
    -f "$override" \
    --profile direct-https --profile direct-mcp down --volumes --remove-orphans \
    >/dev/null 2>&1 || true
  rm -rf "$root"
}
trap cleanup EXIT

token=$(cat "$token_file")
chmod 600 "$token_config"
printf 'header = "Authorization: Bearer %s"\ncacert = "%s"\n' \
  "$token" "$tls_dir/tls.crt" > "$token_config"

docker compose -p "$project" -f "$compose_file" \
  -f "$override" \
  --profile direct-https --profile direct-mcp up -d --force-recreate --wait --wait-timeout 90

curl --fail --silent --show-error --cacert "$tls_dir/tls.crt" "$api_url/health/ready" >/dev/null
curl --fail --silent --show-error --config "$token_config" "$api_url/brief" > "$brief"
grep -F 'cadastre brief' "$brief" >/dev/null

curl --fail --silent --show-error --config "$token_config" \
  -H 'Content-Type: application/json' -X POST \
  --data '{"kind":"network","id":"stack-smoke-network","record":{"notes":"stack smoke annotation"},"reason":"stack smoke"}' \
  "$api_url/annotate" >/dev/null
curl --fail --silent --show-error --config "$token_config" \
  "$api_url/lookup/stack-smoke-network?kind=network" > "$lookup"
grep -F 'stack smoke annotation' "$lookup" >/dev/null

mcp_payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28"}}'
curl --fail --silent --show-error --config "$token_config" \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -X POST --data "$mcp_payload" "$mcp_url" > "$mcp_response"
grep -F 'serverInfo' "$mcp_response" >/dev/null

curl --fail --silent --show-error "$gui_url/" >/dev/null
curl --fail --silent --show-error "$gui_url/runtime-config.js" >/dev/null

# Recreate services and verify the annotation survives the restart.
docker compose -p "$project" -f "$compose_file" \
  -f "$override" \
  --profile direct-https --profile direct-mcp up -d --force-recreate --wait --wait-timeout 90
curl --fail --silent --show-error --config "$token_config" \
  "$api_url/lookup/stack-smoke-network?kind=network" > "$lookup"
grep -F 'stack smoke annotation' "$lookup" >/dev/null

echo "Cadastre stack smoke passed for API, MCP, GUI, annotation, and restart persistence"
