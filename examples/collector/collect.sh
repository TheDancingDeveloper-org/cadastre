#!/usr/bin/env sh
# The scheduled job that populates observed.sqlite3.
#
# Cadastre does not daemonize (DESIGN §2.2): this starts, collects, and
# exits. Run it on the **collector host** — the box that can reach the ingress
# admin API, the forge, and the hypervisor. It writes only observed SQLite
# evidence in the configured data directory.
#
# It is deliberately dumb. Every decision it could make wrong — which sources,
# how fresh, what counts as drift — belongs in the plugin configuration, not here.
#
# Environment:
#   CADASTRE_DATA_DIR  initialized runtime data directory
#   CADASTRE_P_*       one read-only credential per plugin, from the EnvironmentFile

set -eu

DATA_DIR=${CADASTRE_DATA_DIR:?set CADASTRE_DATA_DIR to the initialized data directory}

# Collectors that cannot reach their plugin keep their previous evidence and
# mark the source stale (DESIGN §2.2). That evidence is kept either way, so a
# single unreachable plugin must not abort the rest of this run — but it must
# still reach the timer, which detects a failed collection only through this
# script's exit status. Hence: record it, finish the run, exit with it.
collect_status=0
cadastre --data-dir "$DATA_DIR" collect --exit-code || collect_status=$?

# drift itself writes nothing (it reports and stops — DESIGN §1.3). The
# generated drift.json in the layout is generated here, by redirection.
cadastre --data-dir "$DATA_DIR" drift --json

# Report drift to the journal. Drift never decides this script's exit status:
# it is a human decision, and a timer that goes red on every divergence is a
# timer nobody reads. A failed *collection* is not a human decision.
cadastre --data-dir "$DATA_DIR" drift || true

exit "$collect_status"
