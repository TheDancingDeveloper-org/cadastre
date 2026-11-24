"""k3s node collector (WI-4564), against a recorded NodeList — no cluster."""

from __future__ import annotations

import base64
import http.server
import json
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from cadastre.core.errors import Located
from cadastre.core.observed import parse_source
from cadastre.plugins.collectors import infra_k3s
from cadastre.plugins.collectors.http import HttpError

NODES = json.loads(
    (Path(__file__).parent / "fixtures" / "k3s" / "nodes.json").read_text("utf-8")
)


def _hosts(options: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    result = infra_k3s.transform(NODES, options or {})
    parse_source(
        {"entities": result["entities"]},
        Located("fixture"),
        extensions={"host": {"x-k3s"}},
    )
    return {h["id"]: h for h in result["entities"]["host"]}


def test_roles_come_from_node_labels() -> None:
    hosts = _hosts()
    assert hosts["winrarhost1"]["x-k3s"]["roles"] == ["control-plane", "etcd"]
    assert hosts["winrarhost3"]["x-k3s"]["roles"] == ["worker"]  # unlabelled agent
    assert hosts["winrarhost4"]["x-k3s"]["roles"] == ["worker"]


def test_lan_and_tailnet_addresses_are_aliases() -> None:
    host = _hosts()["winrarhost1"]
    assert host["aliases"] == ["192.168.1.11", "100.64.0.11"]
    assert "winrarhost1" not in host["aliases"]  # the id is not its own alias
    assert host["role"] == "server"


def test_pressure_conditions_and_unknown_status() -> None:
    hosts = _hosts()
    assert hosts["winrarhost1"]["x-k3s"]["ready"] is True
    assert hosts["winrarhost1"]["x-k3s"]["disk_pressure"] is False
    assert hosts["winrarhost3"]["x-k3s"]["disk_pressure"] is True
    down = hosts["winrarhost4"]["x-k3s"]
    assert down["ready"] is False
    assert down["memory_pressure"] is True  # Unknown is not "fine"
    assert down["unschedulable"] is True


def test_capacity_is_converted() -> None:
    hosts = _hosts()
    assert hosts["winrarhost1"]["resources"] == {
        "cpu_cores": 8,
        "memory_gb": 31,
        "disk_gb": 456,
    }
    assert hosts["winrarhost3"]["resources"]["cpu_cores"] == 16
    assert hosts["winrarhost1"]["x-k3s"]["pod_capacity"] == 110


def test_id_map_and_network() -> None:
    hosts = _hosts({"id_map": {"winrarhost1": "wh1"}, "network": "lan"})
    assert "wh1" in hosts and "winrarhost1" not in hosts
    assert hosts["wh1"]["x-k3s"]["node_name"] == "winrarhost1"
    assert hosts["wh1"]["reachable_from"] == ["lan"]
    assert "winrarhost1" in hosts["wh1"]["aliases"]


def test_quantities() -> None:
    assert infra_k3s._quantity("3900m") == pytest.approx(3.9)
    assert infra_k3s._quantity("1Gi") == 1024**3
    assert infra_k3s._quantity("bogus") is None


# -- kubeconfig and the wire ------------------------------------------------


def _kubeconfig(tmp_path: Path, server: str, token: str = "s3cret-token") -> Path:
    doc = {
        "current-context": "ro",
        "contexts": [{"name": "ro", "context": {"cluster": "c", "user": "u"}}],
        "clusters": [{"name": "c", "cluster": {"server": server}}],
        "users": [{"name": "u", "user": {"token": token}}],
    }
    path = tmp_path / "kubeconfig"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_path_comes_from_config_or_the_named_env_var(tmp_path: Path) -> None:
    assert infra_k3s.kubeconfig_path({"kubeconfig": "/x"}, {}) == Path("/x")
    env = {"K8S_AGENT_READONLY_KUBECONFIG": "/y"}
    assert infra_k3s.kubeconfig_path({}, env) == Path("/y")
    assert infra_k3s.kubeconfig_path({"kubeconfig_env": "OTHER"}, {"OTHER": "/z"}) == (
        Path("/z")
    )
    with pytest.raises(HttpError) as err:
        infra_k3s.kubeconfig_path({}, {})
    assert err.value.kind == "invalid_config"


def test_errors_never_carry_kubeconfig_contents(tmp_path: Path) -> None:
    path = tmp_path / "kc"
    path.write_text("clusters: [s3cret-token", encoding="utf-8")
    with pytest.raises(HttpError) as err:
        infra_k3s.load_connection(path)
    assert "s3cret-token" not in err.value.message
    path.write_text(
        yaml.safe_dump({"clusters": [], "users": [], "contexts": []}), encoding="utf-8"
    )
    with pytest.raises(HttpError):
        infra_k3s.load_connection(path)


def test_client_cert_data_must_be_base64(tmp_path: Path) -> None:
    doc = {
        "current-context": "ro",
        "contexts": [{"name": "ro", "context": {"cluster": "c", "user": "u"}}],
        "clusters": [{"name": "c", "cluster": {"server": "https://k:6443"}}],
        "users": [
            {
                "name": "u",
                "user": {
                    "client-certificate-data": "!!notb64",
                    "client-key-data": base64.b64encode(b"k").decode(),
                },
            }
        ],
    }
    path = tmp_path / "kc"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(HttpError) as err:
        infra_k3s.load_connection(path)
    assert err.value.kind == "invalid_config"


def test_fetch_nodes_is_a_bearer_get_of_api_v1_nodes(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization", "")
            body = json.dumps(NODES).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = infra_k3s.load_connection(
            _kubeconfig(tmp_path, f"http://127.0.0.1:{server.server_port}")
        )
        payload = infra_k3s.fetch_nodes(conn, timeout=5)
    finally:
        server.shutdown()
    assert seen == {"path": "/api/v1/nodes", "auth": "Bearer s3cret-token"}
    assert len(payload["items"]) == 4


def test_forbidden_is_unauthorized(tmp_path: Path) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(403)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = infra_k3s.load_connection(
            _kubeconfig(tmp_path, f"http://127.0.0.1:{server.server_port}")
        )
        with pytest.raises(HttpError) as err:
            infra_k3s.fetch_nodes(conn, timeout=5)
    finally:
        server.shutdown()
    assert err.value.kind == "unauthorized"
    assert "s3cret-token" not in err.value.message
