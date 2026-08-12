#!/usr/bin/env sh
# A plugin in 20 lines of shell.
#
# This is the adoption unlock (DESIGN §4.4): anyone integrates anything with a
# short script, and nobody writes Python to try the tool. It is also how you
# find out which integrations deserve to be real ones — a script that keeps
# growing is a plugin asking to be written.
#
# Wire protocol: one JSON object in on stdin, one JSON object out on stdout,
# exit 0. Diagnostics go to stderr. Nothing but the JSON object on stdout.
#
# Register it as:
#
#   sources:
#     - id: containers
#       command: [sh, /path/to/hosts-from-ssh.sh]
#       methods: [inventory.list]
#       env: [SSH_AUTH_SOCK]

set -eu

# The request is on stdin. `params` may be logged, so it never carries a
# credential; this one needs no configuration at all.
cat > /dev/null

AS_OF=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Anything that can produce JSON works here. Real ones would run, say,
# `ssh app-01 docker ps --format json` and reshape the output.
cat <<EOF
{"v":1,"ok":true,"as_of":"${AS_OF}","warnings":[],
 "result":{"entities":{"host":[
   {"id":"app-01","role":"container-host","reachable_from":["lab-net"]}
 ]}}}
EOF
