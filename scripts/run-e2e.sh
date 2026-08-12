#!/bin/sh
# Start the isolated synthetic catalog and run every network/browser E2E check.
set -eu

artifact_dir=${CADASTRE_E2E_ARTIFACT_DIR:-.ci-artifacts/e2e}
environment_file="$artifact_dir/environment"
server_log="$artifact_dir/server.log"
mkdir -p "$artifact_dir"
: >"$environment_file"

uv run --frozen --extra dev --extra mcp-server python scripts/e2e_stack.py \
  --environment-file "$environment_file" >"$server_log" 2>&1 &
server_pid=$!

cleanup() {
  kill "$server_pid" >/dev/null 2>&1 || true
  wait "$server_pid" >/dev/null 2>&1 || true
  if [ "$e2e_status" -ne 0 ]; then
    echo "Synthetic Cadastre server log:"
    sed -n '1,400p' "$server_log"
  fi
}
e2e_status=1
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

attempt=0
while [ "$attempt" -lt 30 ]; do
  if [ -s "$environment_file" ]; then
    break
  fi
  if ! kill -0 "$server_pid" >/dev/null 2>&1; then
    echo "Synthetic Cadastre environment stopped before becoming ready" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done
test -s "$environment_file"

set -a
# Values are generated loopback URLs without spaces or shell metacharacters.
. "$environment_file"
set +a

(cd ui && npm run test:e2e)
e2e_status=0
