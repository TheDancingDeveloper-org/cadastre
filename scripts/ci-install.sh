#!/bin/sh
set -eu

# Every CI container is isolated. Export from the committed lockfile so the
# authoritative gates test the same dependency graph that was reviewed.
extra=${1:-dev,mcp-server}
case "$extra" in
  dev,mcp-server) export_args="--extra dev --extra mcp-server" ;;
  dev,mcp) export_args="--extra dev --extra mcp-server" ;;
  dev) export_args="--extra dev" ;;
  *) echo "unsupported CI extra set: $extra" >&2; exit 2 ;;
esac
uv export --frozen $export_args --no-emit-project --format requirements.txt \
  --output-file /tmp/cadastre-requirements.txt
uv pip install --system --strict --requirement /tmp/cadastre-requirements.txt
uv pip install --system --no-deps -e .
