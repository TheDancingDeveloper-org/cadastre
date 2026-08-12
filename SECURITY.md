# Deployment security profiles

Cadastre is a catalog service, not a Broker and not a deployment engine. The
container has no Docker socket, no collector credentials, and no infrastructure
write capability.

The profile is chosen with `--profile` on `serve` and `security-check`. There
are five, and these are their exact names:

| `--profile` | Use |
|---|---|
| `loopback-development` | The default. Binds loopback; suitable only for a local operator |
| `direct-https` | Cadastre-owned TLS. Requires operator-provided certificates, scoped bearer tokens, and the explicit non-loopback flag |
| `mtls` | `direct-https` plus a CA and client-certificate validation; map the verified certificate principal to explicit scopes |
| `trusted-proxy` | Binds a private backend and accepts identity only from configured trusted proxy networks with a configured HMAC identity secret |
| `development-plaintext` | Unauthenticated plaintext, for development only. Never an estate deployment |

Every remote profile must specify endpoint identity, trust anchor, audience,
principal/scope mapping, allowed operations, request limits, Host and Origin
allowlists, and an explicit write decision. Missing identity or trust material
fails closed. Run `cadastre security-check` before exposing an endpoint.

Agent access has two separate components: the Cadastre MCP server runs with the
application stack, while an agent either uses a native remote MCP client or
installs `cadastre-mcp-remote` as a local stdio bridge. The bridge is remote-only
and must never open a local catalog fallback. The GUI is likewise an unprivileged
HTTP/API client and never receives the SQLite volume or a Broker path.

The data directory contains two separate SQLite databases. Take a transaction-
consistent `cadastre backup` before upgrades, retain encrypted/off-host copies,
and test restoration into a fresh directory. `DEPLOYMENT.md` §7.1 is the
procedure that follows that backup. Export bundles are review and
recovery aids; they do not replace SQLite backups because they omit transaction
history and may omit observed history by operator choice.

Fresh-user path:

1. Pull a published image by signed immutable digest.
2. Create the named persistent volume and run `cadastre init --data-dir /var/lib/cadastre`.
3. Choose a profile: `loopback-development`, `direct-https`, `mtls`, or
   `trusted-proxy`.
4. Verify readiness and run the first integrity check.
5. Configure optional collectors as separate jobs with separate read-only mounts.
6. Take and verify the first off-host backup.
