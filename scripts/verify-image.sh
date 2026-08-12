#!/bin/sh
set -eu
image=${1:?usage: verify-image.sh IMAGE}
docker image inspect "$image" >/dev/null
test "$(docker run --rm --entrypoint id "$image" -u)" = 10001
! docker run --rm --entrypoint sh "$image" -c 'command -v git || command -v docker'
! docker run --rm "$image" sh -c 'test -e /var/run/docker.sock'
