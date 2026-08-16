#!/bin/sh
set -eu

image=${1:?usage: container-smoke.sh IMAGE}
api_url=${CADASTRE_SMOKE_API_URL:-}
mcp_url=${CADASTRE_SMOKE_MCP_URL:-}
gui_url=${CADASTRE_SMOKE_GUI_URL:-}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname "$script_dir")
volume="cadastre-smoke-$$"
restored_volume="cadastre-smoke-restored-$$"
bundle_volume="cadastre-smoke-bundle-$$"
bundle="$root/tests/fixtures/container-smoke-bundle"
test -r "$bundle/manifest.json"
smoke_curl_config=$(mktemp)
smoke_brief=$(mktemp)
smoke_lookup=$(mktemp)
smoke_mcp=$(mktemp)
cleanup() {
  rm -f "$smoke_curl_config" "$smoke_brief" "$smoke_lookup" "$smoke_mcp"
  docker volume rm "$volume" "$restored_volume" "$bundle_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$volume" >/dev/null
docker volume create "$restored_volume" >/dev/null
docker volume create "$bundle_volume" >/dev/null

# The bundle reaches the container through the Docker API, not through a bind
# mount of this checkout. The release job runs on a Docker-in-Docker worker,
# where the daemon does not share this filesystem: `-v "$bundle:..."` is
# resolved by the daemon, finds nothing at that path, and mounts an empty
# directory — so the readable-here check above passes and `init` then reports
# no bundle. `docker cp` streams the files over the socket and does not care
# whose filesystem they came from. Every other volume in this script is
# already a named one for the same reason.
staging=$(docker create -v "$bundle_volume:/tmp/container-smoke-bundle" \
  --entrypoint sh "$image" -c true)
docker cp "$bundle/." "$staging:/tmp/container-smoke-bundle/"
docker rm "$staging" >/dev/null

docker run --rm \
  --user 10001:10001 \
  -v "$volume:/var/lib/cadastre" \
  -v "$bundle_volume:/tmp/container-smoke-bundle:ro" \
  "$image" init --data-dir /var/lib/cadastre \
  --from-bundle /tmp/container-smoke-bundle --json
docker run --rm \
  --user 10001:10001 \
  -v "$volume:/var/lib/cadastre" \
  "$image" status --data-dir /var/lib/cadastre --json
docker run --rm \
  --user 10001:10001 \
  -v "$volume:/var/lib/cadastre" \
  "$image" integrity-check --data-dir /var/lib/cadastre --json
docker run --rm \
  --user 10001:10001 \
  -v "$volume:/var/lib/cadastre" \
  "$image" annotate network:container-smoke-network \
  --data-dir /var/lib/cadastre --principal smoke --reason lifecycle \
  tags=smoke,persisted --json
docker run --rm \
  --user 10001:10001 \
  -v "$volume:/var/lib/cadastre" \
  "$image" lookup container-smoke-network --kind network \
  --data-dir /var/lib/cadastre --json
docker run --rm \
  --user 10001:10001 \
  -v "$volume:/var/lib/cadastre" \
  "$image" backup --data-dir /var/lib/cadastre --output /var/lib/cadastre/smoke-backup
docker run --rm \
  --user 10001:10001 \
  -v "$restored_volume:/var/lib/cadastre" \
  -v "$volume:/backup-source:ro" \
  "$image" restore --data-dir /var/lib/cadastre \
  --input /backup-source/smoke-backup --json
docker run --rm \
  --user 10001:10001 \
  -v "$restored_volume:/var/lib/cadastre" \
  "$image" lookup container-smoke-network --kind network \
  --data-dir /var/lib/cadastre --json

test "$(docker run --rm --entrypoint id "$image" -u)" = 10001
! docker run --rm --entrypoint sh "$image" -c 'command -v git || command -v docker'
! docker run --rm --entrypoint sh "$image" -c 'test -e /var/run/docker.sock'

# Optional networked stack checks are enabled by the release runner after it
# starts the documented Compose profile. They never weaken the offline store
# checks above and carry credentials only through the runner environment.
if test -n "$api_url" || test -n "$mcp_url"; then
  test -n "${CADASTRE_SMOKE_TOKEN_FILE:?set token file for API/MCP smoke}"
  test -r "$CADASTRE_SMOKE_TOKEN_FILE"
  smoke_token=$(cat "$CADASTRE_SMOKE_TOKEN_FILE")
  chmod 600 "$smoke_curl_config"
  printf 'header = "Authorization: Bearer %s"\n' "$smoke_token" > "$smoke_curl_config"
fi
if test -n "$api_url"; then
  curl --fail --silent --show-error "$api_url/health/ready" >/dev/null
  curl --fail --silent --show-error --config "$smoke_curl_config" "$api_url/brief" > "$smoke_brief"
  grep -F 'cadastre brief' "$smoke_brief" >/dev/null
  curl --fail --silent --show-error --config "$smoke_curl_config" \
    -H 'Content-Type: application/json' -X POST \
    --data '{"kind":"network","id":"container-smoke-network","record":{"notes":"network smoke annotation"},"reason":"container smoke"}' \
    "$api_url/annotate" >/dev/null
  curl --fail --silent --show-error --config "$smoke_curl_config" \
    "$api_url/lookup/container-smoke-network?kind=network" > "$smoke_lookup"
  grep -F 'network smoke annotation' "$smoke_lookup" >/dev/null
fi
if test -n "$mcp_url"; then
  mcp_payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28"}}'
  curl --fail --silent --show-error --config "$smoke_curl_config" \
    -H 'Content-Type: application/json' -H 'Accept: application/json' \
    -X POST --data "$mcp_payload" "$mcp_url" > "$smoke_mcp"
  grep -F 'serverInfo' "$smoke_mcp" >/dev/null
fi
if test -n "$gui_url"; then
  curl --fail --silent --show-error "$gui_url/" >/dev/null
  curl --fail --silent --show-error "$gui_url/runtime-config.js" >/dev/null
fi
