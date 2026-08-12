#!/bin/sh
# Verify the published wheel, not imports from the source checkout.
set -eu

wheel=${1:?usage: package-install-smoke.sh WHEEL}
test -s "$wheel"
root=$(mktemp -d)
cleanup() { rm -rf "$root"; }
trap cleanup EXIT

python3 -m venv "$root/venv"
"$root/venv/bin/python" -m pip install --quiet "${wheel}[mcp-client]"
test -x "$root/venv/bin/cadastre"
test -x "$root/venv/bin/cadastre-mcp-remote"
test -x "$root/venv/bin/cadastre-mcp"
"$root/venv/bin/python" -c 'import mcp'

"$root/venv/bin/python" - <<'PY'
from importlib.metadata import distribution

metadata = distribution("cadastre")
extras = {line.split(";", 1)[0] for line in metadata.requires or ()}
assert any("mcp" in line and "extra == 'mcp-server'" in line for line in metadata.requires or ())
assert any("mcp" in line and "extra == 'mcp-client'" in line for line in metadata.requires or ())
assert metadata.version
PY

set +e
output=$(env -u CADASTRE_MCP_URL -u CADASTRE_HTTP_URL \
  CADASTRE_CATALOG="$root/should-not-exist" \
  "$root/venv/bin/cadastre-mcp-remote" 2>&1)
status=$?
set -e
test "$status" -eq 2
printf '%s\n' "$output" | grep -F 'CADASTRE_MCP_URL is required' >/dev/null
! printf '%s\n' "$output" | grep -F 'should-not-exist' >/dev/null
