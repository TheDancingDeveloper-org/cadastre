"""Kubernetes node collector (k3s).

Observes the cluster's nodes through the Kubernetes API with a read-only
credential and reports each as a `host`: its roles (control-plane, etcd,
worker), its LAN and tailnet addresses as `aliases`, its Ready / disk / memory
/ PID pressure conditions and its capacity.

The credential is a kubeconfig. Its *path* arrives in configuration — either
`config.kubeconfig` or, as deployments do, `config.kubeconfig_env` naming the
environment variable that holds the path (default
`K8S_AGENT_READONLY_KUBECONFIG`). The file's contents are never logged, echoed
or placed in an error message. Only `GET /api/v1/nodes` is called; the service
account is expected to be read-only and the collector has no write path.

Vendor nouns (`node`, `control-plane`, `kubelet`) stay in this file and in the
`x-k3s` evidence block. What leaves as neutral fields is `host`, `aliases`,
`resources` and `reachable_from`.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from cadastre.core.provenance import format_timestamp
from cadastre.plugins.collectors import serve_collector
from cadastre.plugins.collectors.http import DEFAULT_TIMEOUT, HttpError
from cadastre.plugins.collectors.http import _from_status as from_status
from cadastre.plugins.protocol import Reply, Request, ok

NAME = "infra-k3s"
VERSION = "1"
CAPABILITIES = ("Inventory",)

DEFAULT_KUBECONFIG_ENV = "K8S_AGENT_READONLY_KUBECONFIG"

#: Node label -> role.
_ROLE_LABELS = {
    "node-role.kubernetes.io/control-plane": "control-plane",
    "node-role.kubernetes.io/master": "control-plane",
    "node-role.kubernetes.io/etcd": "etcd",
    "node-role.kubernetes.io/worker": "worker",
}
_ROLE_PREFIX = "node-role.kubernetes.io/"

#: Pressure conditions; True means the node is under that pressure.
_PRESSURE = {
    "DiskPressure": "disk_pressure",
    "MemoryPressure": "memory_pressure",
    "PIDPressure": "pid_pressure",
}

_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}
_BYTES_PER_GB = 1024**3


def _quantity(value: Any) -> float | None:
    """A Kubernetes resource quantity (`16Gi`, `3900m`, `8`) as a number."""
    text = str(value if value is not None else "").strip()
    match = re.fullmatch(r"([0-9.]+)(m|Ki|Mi|Gi|Ti|k|K|M|G|T)?", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return number / 1000
    return number * _UNITS.get(unit or "", 1)


def _gb(value: Any) -> int | None:
    number = _quantity(value)
    return max(1, round(number / _BYTES_PER_GB)) if number else None


def _int(value: Any) -> int | None:
    number = _quantity(value)
    return round(number) if number else None


def _roles(labels: dict[str, Any]) -> list[str]:
    found = {role for label, role in _ROLE_LABELS.items() if label in labels}
    if not found:
        found = {
            label.removeprefix(_ROLE_PREFIX)
            for label in labels
            if label.startswith(_ROLE_PREFIX) and label != _ROLE_PREFIX
        }
    # k3s agents carry no role label at all; that is a plain worker.
    return sorted(found) if found else ["worker"]


def _conditions(status: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cond in status.get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        kind, state = cond.get("type"), cond.get("status")
        if state not in ("True", "False", "Unknown"):
            continue
        if kind == "Ready":
            out["ready"] = state == "True"
            if cond.get("reason"):
                out["ready_reason"] = str(cond["reason"])
        elif kind in _PRESSURE:
            # Unknown is not "fine": a kubelet that stopped reporting says
            # nothing about pressure, so it is recorded as present.
            out[_PRESSURE[str(kind)]] = state != "False"
    return out


def _addresses(status: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(hostnames, ip addresses), in API order, deduplicated."""
    names: list[str] = []
    ips: list[str] = []
    for entry in status.get("addresses") or []:
        if not isinstance(entry, dict) or not entry.get("address"):
            continue
        address = str(entry["address"])
        kind = entry.get("type")
        if kind in ("InternalIP", "ExternalIP"):
            if address not in ips:
                ips.append(address)
        elif kind in ("Hostname", "InternalDNS", "ExternalDNS") and (
            address not in names
        ):
            names.append(address)
    return names, ips


def transform(payload: Any, options: dict[str, Any]) -> dict[str, Any]:
    """A `NodeList` -> host entities.

    `options.id_map` renames a node name to the catalog host id and
    `options.network` adds `reachable_from`.
    """
    id_map = {str(k): str(v) for k, v in (options.get("id_map") or {}).items()}
    network = options.get("network")
    items = payload.get("items") if isinstance(payload, dict) else payload
    hosts = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        name = str(meta.get("name") or "")
        if not name:
            continue
        host_id = id_map.get(name, name)
        names, ips = _addresses(status)
        aliases = [a for a in [*names, *ips] if a.casefold() != host_id.casefold()]
        entity: dict[str, Any] = {"id": host_id, "role": "server"}
        if aliases:
            entity["aliases"] = aliases
        if network:
            entity["reachable_from"] = [str(network)]
        capacity = status.get("capacity") or {}
        resources = {
            "cpu_cores": _int(capacity.get("cpu")),
            "memory_gb": _gb(capacity.get("memory")),
            "disk_gb": _gb(capacity.get("ephemeral-storage")),
        }
        resources = {k: v for k, v in resources.items() if v}
        if resources:
            entity["resources"] = resources

        x: dict[str, Any] = {"roles": _roles(meta.get("labels") or {})}
        x.update(_conditions(status))
        if ips:
            x["addresses"] = ips
        if name != host_id:
            x["node_name"] = name
        if (item.get("spec") or {}).get("unschedulable"):
            x["unschedulable"] = True
        pods = _int(capacity.get("pods"))
        if pods:
            x["pod_capacity"] = pods
        info = status.get("nodeInfo") or {}
        for key, out in (
            ("kubeletVersion", "kubelet_version"),
            ("osImage", "os_image"),
            ("kernelVersion", "kernel_version"),
            ("architecture", "architecture"),
        ):
            if info.get(key):
                x[out] = str(info[key])
        entity["x-k3s"] = x
        hosts.append(entity)
    hosts.sort(key=lambda h: str(h["id"]))
    return {"entities": {"host": hosts}}


# -- the API call ----------------------------------------------------------


def _b64(data: str, what: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise HttpError("invalid_config", f"kubeconfig: bad base64 in {what}") from exc


def _named(entries: Any, name: Any, key: str) -> dict[str, Any]:
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            value = entry.get(key)
            return value if isinstance(value, dict) else {}
    return {}


def kubeconfig_path(config: dict[str, Any], environ: Mapping[str, str]) -> Path:
    """Where the kubeconfig is, from config; never its contents."""
    if config.get("kubeconfig"):
        return Path(str(config["kubeconfig"]))
    env_name = str(config.get("kubeconfig_env") or DEFAULT_KUBECONFIG_ENV)
    value = environ.get(env_name)
    if not value:
        raise HttpError(
            "invalid_config",
            f"no kubeconfig: config.kubeconfig is unset and {env_name!r} is not "
            "set in this process's environment (it holds the file's path).",
        )
    return Path(value)


def load_connection(path: Path) -> dict[str, Any]:
    """Parse a kubeconfig into server, TLS and credential material.

    Error messages carry the path and the problem, never a value from the file.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HttpError("invalid_config", f"kubeconfig {path}: unreadable") from exc
    except yaml.YAMLError as exc:
        raise HttpError("invalid_config", f"kubeconfig {path}: not valid YAML") from exc
    if not isinstance(doc, dict):
        raise HttpError("invalid_config", f"kubeconfig {path}: not a mapping")
    contexts = doc.get("contexts")
    ctx = _named(contexts, doc.get("current-context"), "context")
    if not ctx and isinstance(contexts, list) and len(contexts) == 1:
        first = contexts[0]
        ctx = (first.get("context") or {}) if isinstance(first, dict) else {}
    cluster = _named(doc.get("clusters"), ctx.get("cluster"), "cluster")
    user = _named(doc.get("users"), ctx.get("user"), "user")
    server = str(cluster.get("server") or "").rstrip("/")
    if not server.startswith(("http://", "https://")):
        raise HttpError("invalid_config", f"kubeconfig {path}: no usable server")
    conn: dict[str, Any] = {
        "server": server,
        "insecure": bool(cluster.get("insecure-skip-tls-verify")),
        "ca": None,
        "ca_file": None,
        "token": None,
        "cert": None,
        "key": None,
    }
    if cluster.get("certificate-authority-data"):
        conn["ca"] = _b64(str(cluster["certificate-authority-data"]), "CA").decode()
    elif cluster.get("certificate-authority"):
        conn["ca_file"] = str(cluster["certificate-authority"])
    if user.get("token"):
        conn["token"] = str(user["token"])
    if user.get("client-certificate-data") and user.get("client-key-data"):
        conn["cert"] = _b64(str(user["client-certificate-data"]), "cert")
        conn["key"] = _b64(str(user["client-key-data"]), "key")
    if not conn["token"] and not conn["cert"]:
        raise HttpError("invalid_config", f"kubeconfig {path}: no usable credential")
    return conn


def _base_context(conn: dict[str, Any]) -> ssl.SSLContext:
    if conn["ca"]:
        ctx = ssl.create_default_context(cadata=conn["ca"])
    elif conn["ca_file"]:
        ctx = ssl.create_default_context(cafile=conn["ca_file"])
    else:
        ctx = ssl.create_default_context()
    if conn["insecure"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


@contextlib.contextmanager
def _ssl_context(conn: dict[str, Any]) -> Iterator[ssl.SSLContext]:
    try:
        ctx = _base_context(conn)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise HttpError("invalid_config", "kubeconfig: unusable CA") from exc
    if not conn["cert"]:
        yield ctx
        return
    # ssl can only load a client certificate from files: 0600, removed at once.
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = Path(tmp, "c.pem"), Path(tmp, "k.pem")
        for target, data in ((cert, conn["cert"]), (key, conn["key"])):
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        try:
            ctx.load_cert_chain(str(cert), str(key))
        except (ssl.SSLError, OSError) as exc:
            raise HttpError("invalid_config", "kubeconfig: bad client cert") from exc
        yield ctx


def fetch_nodes(conn: dict[str, Any], timeout: int = DEFAULT_TIMEOUT) -> Any:
    """GET /api/v1/nodes — the only call this collector makes."""
    url = conn["server"] + "/api/v1/nodes"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if conn["token"]:
        request.add_header("Authorization", f"Bearer {conn['token']}")
    with _ssl_context(conn) as ctx:
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ctx) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise from_status(exc.code, url) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise HttpError(
                "unreachable", f"{url}: unreachable", retryable=True
            ) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError("internal", f"{url}: response was not JSON") from exc


def _collect(request: Request) -> Reply:
    path = kubeconfig_path(request.config, os.environ)
    conn = load_connection(path)
    timeout = int(request.config.get("timeout_seconds", DEFAULT_TIMEOUT))
    payload = fetch_nodes(conn, timeout)
    return ok(
        transform(payload, request.config), format_timestamp(datetime.now(tz=UTC))
    )


def main() -> int:
    return serve_collector(
        name=NAME,
        version=VERSION,
        capabilities=CAPABILITIES,
        methods={"inventory.list": _collect},
        entities=("host",),
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
