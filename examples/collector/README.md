# The scheduled collection job

`cadastre collect` is a process that starts, collects, and exits — Cadastre does
not daemonize (DESIGN §2.2). Something external has to run it on a schedule.
This directory is that something, as a systemd timer.

```
[collector host]  this timer, read-only creds per plugin
        │  writes observed.sqlite3
        ▼
[persistent data directory]
        ▼
[query process]  cadastre brief / context-for / check
```

The split is the security property, not an operational preference. The query
process receives no collector credentials; plugin credentials live only in the
collector job environment.

## Install

```sh
useradd --system --home-dir /var/lib/cadastre cadastre
install -d -o cadastre -g cadastre -m 0700 /var/lib/cadastre
sudo -u cadastre cadastre init --data-dir /var/lib/cadastre

uv tool install cadastre
install -m 0755 collect.sh /usr/local/bin/cadastre-collect

install -d -m 0700 /etc/cadastre
install -m 0400 -o cadastre collect.env.sample /etc/cadastre/collect.env
$EDITOR /etc/cadastre/collect.env          # fill in the read-only tokens
# Mount or copy the collector configuration as /var/lib/cadastre/plugins.yaml.

install -m 0644 cadastre-collect.service cadastre-collect.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cadastre-collect.timer
```

Verify before trusting it:

```sh
sudo -u cadastre CADASTRE_DATA_DIR=/var/lib/cadastre cadastre sources
sudo -u cadastre CADASTRE_DATA_DIR=/var/lib/cadastre cadastre collect --dry-run
systemctl start cadastre-collect.service && journalctl -u cadastre-collect
```

`sources` is the one to run first. It reports what each plugin says about
itself, so a bad endpoint or a missing token surfaces as a handshake failure
rather than as a source that quietly renders stale forever.

The collector writes only the observed database. Use `cadastre backup` for
transaction-consistent recovery; use `cadastre export` when a reviewable bundle
is needed.

## Credentials requiring verification

`collect.env.sample` names one variable per plugin. The two Tailscale references
now exist as candidate Infisical entries, but their scopes, validity, and
runtime access still require verification. The estate still needs:

- a **Cloudflare Zone:Read** token — DNS collection currently borrows Caddy's
  DNS-01 token, which can *edit* records;
- a **Tailscale API token or OAuth client** with `devices:core:read` — an
  auth key joins a node and cannot enumerate them, so the two are not
  interchangeable. The reference secret location is Infisical `Infra` / `Prod`
  / `TailScaleAPI_KEY`; expose it to the collector only as
  `CADASTRE_P_VPN_TOKEN`;

- a **Tailscale auth key** for the separate access path. The reference secret
  location is Infisical `Infra` / `Prod` / `TailScaleAUTH_KEY`; this is not the
  same credential as `CADASTRE_P_VPN_TOKEN` and must not be sent to the VPN
  inventory API;
- a **read-only identity for the secret manager** — collection borrows one
  with read/write on every project.

None of these block installation. Each blocks one source or access path, which
must render as a handshake/access failure in `cadastre sources` rather than as
a silent gap.

## Interval

`OnCalendar=hourly` is a starting point, not a recommendation. The rule: a
source collected less often than its freshness threshold (D4, `freshness:` in
the plugin configuration) renders as STALE, correctly and permanently. Match
the interval to the tightest threshold you actually act on, and loosen the
thresholds you don't.

## If you would rather use CI

A cron-triggered pipeline works identically — same script, same environment
variables — provided the runner is on a host that can reach your plugins. It
usually is not: the ingress admin API in the sample config is bound to
`127.0.0.1`, and exposing it to a build runner to avoid a timer is a bad trade.
