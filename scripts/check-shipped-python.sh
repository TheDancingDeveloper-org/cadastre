#!/bin/sh
# The interpreter Dockerfile ships must be one the test matrix exercises.
#
# Dependabot derives an update type from the Docker *tag*, so `python:3.14 ->
# python:3.15` arrives looking like a minor bump (#46). Nothing about that PR
# announces that it changes the interpreter production runs on. This check is
# what makes the two files disagree loudly instead of silently: bump the tag
# without adding the version to the matrix and CI fails here, naming both.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
dockerfile="$root/Dockerfile"
workflow="$root/.github/workflows/ci.yaml"

shipped=$(sed -n 's/^FROM python:\([0-9][0-9.]*\)[-@ ].*/\1/p' "$dockerfile" | head -1)
if [ -z "$shipped" ]; then
  echo "check-shipped-python: no 'FROM python:<version>' line in $dockerfile" >&2
  exit 1
fi

matrix=$(sed -n 's/^ *python-version: *\(\[.*\]\) *$/\1/p' "$workflow" | head -1)
if [ -z "$matrix" ]; then
  echo "check-shipped-python: no 'python-version: [...]' matrix in $workflow" >&2
  exit 1
fi

case "$matrix" in
  *"\"$shipped\""*)
    echo "Dockerfile ships python $shipped; the test matrix $matrix covers it."
    ;;
  *)
    echo "Dockerfile ships python $shipped, which the ci test matrix does not" >&2
    echo "cover: $matrix" >&2
    echo >&2
    echo "Add \"$shipped\" to strategy.matrix.python-version in $workflow, or" >&2
    echo "pin Dockerfile back to a tested interpreter. Shipping an untested" >&2
    echo "interpreter is the failure #46 describes." >&2
    exit 1
    ;;
esac
